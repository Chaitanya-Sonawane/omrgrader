// ===========================================================================
// OMR Grader — persistence, sessions, Excel & smart student search layer.
//
// This module is layered ON TOP of the existing scanner. It NEVER touches the
// OMR detection, scoring or PDF logic. It provides:
//   • Scan Sessions (create / rename / reopen / auto-restore last active)
//   • Optimistic-first storage: instant local cache (IndexedDB/localStorage)
//     with async sync to Firebase Firestore + Storage when configured.
//   • Master Excel upload (SheetJS) as the per-session student database.
//   • Bilingual (English + Marathi) smart searchable Student ID / Name fields.
//   • Auto-write of the scanned score into the student's subject column, with
//     overwrite confirmation, then reset for the next scan.
//   • Download Updated Excel preserving the original workbook.
//   • Sync-state indicator (Saving / Saved / Offline / Retry).
//
// It is intentionally defensive: if Firebase is not configured it degrades to
// a local-only device store and everything still works offline.
// ===========================================================================
(function(){
"use strict";

// Exposed namespace so index.html can hook into it.
const OMR = window.OMRSession = window.OMRSession || {};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------
const uid = () => "s_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
const nowIso = () => new Date().toISOString();
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

// ---------------------------------------------------------------------------
// IndexedDB — durable local cache for the optimistic-first store. Sessions and
// their (potentially large) Excel workbooks live here so the app is fully
// usable offline and survives refresh / browser close / crash.
// ---------------------------------------------------------------------------
const DB_NAME = "omr_grader_db";
const DB_VER = 1;
let _dbPromise = null;
function idb(){
  if(_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject)=>{
    if(!("indexedDB" in window)){ reject(new Error("no-indexeddb")); return; }
    const req = indexedDB.open(DB_NAME, DB_VER);
    req.onupgradeneeded = ()=>{
      const db = req.result;
      if(!db.objectStoreNames.contains("sessions")) db.createObjectStore("sessions", {keyPath:"id"});
      if(!db.objectStoreNames.contains("meta"))     db.createObjectStore("meta", {keyPath:"k"});
    };
    req.onsuccess = ()=> resolve(req.result);
    req.onerror   = ()=> reject(req.error);
  });
  return _dbPromise;
}
async function idbPut(store, val){
  const db = await idb();
  return new Promise((res, rej)=>{
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).put(val);
    tx.oncomplete = ()=> res(true);
    tx.onerror = ()=> rej(tx.error);
  });
}
async function idbGet(store, key){
  const db = await idb();
  return new Promise((res, rej)=>{
    const tx = db.transaction(store, "readonly");
    const r = tx.objectStore(store).get(key);
    r.onsuccess = ()=> res(r.result || null);
    r.onerror = ()=> rej(r.error);
  });
}
async function idbAll(store){
  const db = await idb();
  return new Promise((res, rej)=>{
    const tx = db.transaction(store, "readonly");
    const r = tx.objectStore(store).getAll();
    r.onsuccess = ()=> res(r.result || []);
    r.onerror = ()=> rej(r.error);
  });
}
// localStorage fallback (used if IndexedDB is unavailable, e.g. private mode).
const LS_PREFIX = "omr_sess_";
async function saveSessionLocal(sess){
  sess.updatedAt = nowIso();
  try{ await idbPut("sessions", sess); }
  catch(_){ try{ localStorage.setItem(LS_PREFIX + sess.id, JSON.stringify(sess)); }catch(__){} }
}
async function loadAllSessionsLocal(){
  try{ return await idbAll("sessions"); }
  catch(_){
    const out = [];
    for(let i=0;i<localStorage.length;i++){
      const k = localStorage.key(i);
      if(k && k.startsWith(LS_PREFIX)){ try{ out.push(JSON.parse(localStorage.getItem(k))); }catch(__){} }
    }
    return out;
  }
}
async function setActiveIdLocal(id){
  try{ await idbPut("meta", {k:"activeSessionId", v:id}); }
  catch(_){ try{ localStorage.setItem("omr_active_session", id); }catch(__){} }
}
async function getActiveIdLocal(){
  try{ const m = await idbGet("meta", "activeSessionId"); if(m) return m.v; }catch(_){}
  try{ return localStorage.getItem("omr_active_session"); }catch(_){ return null; }
}

// ---------------------------------------------------------------------------
// Sync-state indicator (Saving / Saved / Offline / Retry).
// ---------------------------------------------------------------------------
let _syncEl = null;
function setSync(state, detail){
  if(!_syncEl) _syncEl = document.getElementById("syncState");
  if(!_syncEl) return;
  const map = {
    saving:  {t:"Saving\u2026", c:"sync-saving"},
    saved:   {t:"Saved",       c:"sync-saved"},
    offline: {t:"Offline \u2013 saved on device", c:"sync-offline"},
    retry:   {t:"Retrying sync\u2026", c:"sync-retry"},
  };
  const m = map[state] || map.saved;
  _syncEl.className = "sync-state " + m.c;
  _syncEl.textContent = m.t + (detail ? " \u00b7 " + detail : "");
}

// ---------------------------------------------------------------------------
// Firebase (optional). Uses the compat SDK loaded via <script> in index.html so
// it works without a bundler. All cloud writes are best-effort and async: the
// local cache is always the source of truth for the UI (optimistic-first).
// ---------------------------------------------------------------------------
let fb = {enabled:false, db:null, storage:null};
function initFirebase(){
  if(!window.OMR_FIREBASE_ENABLED || typeof firebase === "undefined") return;
  try{
    firebase.initializeApp(window.OMR_FIREBASE_CONFIG);
    fb.db = firebase.firestore();
    // Offline persistence: keep working through outages and sync on reconnect.
    fb.db.enablePersistence({synchronizeTabs:true}).catch(()=>{});
    try{ fb.storage = firebase.storage(); }catch(_){ fb.storage = null; }
    fb.enabled = true;
  }catch(err){
    console.warn("Firebase init failed, staying local-only:", err);
    fb.enabled = false;
  }
}

// Push a session to Firestore. Uses one document per session plus a `scans`
// subcollection written incrementally (never one huge blob) per the spec.
async function cloudSaveSession(sess){
  if(!fb.enabled) return;
  try{
    setSync("saving");
    const ref = fb.db.collection("sessions").doc(sess.id);
    // Top-level session doc (metadata + student DB + excel pointer). Excel raw
    // bytes are kept locally / in Storage, not inlined into Firestore.
    await ref.set({
      id: sess.id, name: sess.name,
      createdAt: sess.createdAt, updatedAt: sess.updatedAt,
      students: sess.students || [],
      excelMeta: sess.excelMeta || null,
      subjectColumns: sess.subjectColumns || [],
    }, {merge:true});
    setSync("saved");
  }catch(err){
    setSync(navigator.onLine ? "retry" : "offline");
  }
}
// Incremental write of a single scan into the session's `scans` subcollection.
async function cloudSaveScan(sessionId, scan){
  if(!fb.enabled) return;
  try{
    setSync("saving");
    await fb.db.collection("sessions").doc(sessionId)
      .collection("scans").doc(scan.id).set(scan, {merge:true});
    setSync("saved");
  }catch(err){
    setSync(navigator.onLine ? "retry" : "offline");
  }
}
async function cloudLoadSessions(){
  if(!fb.enabled) return [];
  try{
    const snap = await fb.db.collection("sessions").orderBy("updatedAt","desc").get();
    const out = [];
    for(const doc of snap.docs){
      const s = doc.data();
      const scansSnap = await fb.db.collection("sessions").doc(doc.id).collection("scans").get();
      s.scans = scansSnap.docs.map(d=> d.data());
      out.push(s);
    }
    return out;
  }catch(err){ return []; }
}

// ===========================================================================
// Session model & state
// ===========================================================================
// A session: {id, name, createdAt, updatedAt, students[], excelMeta, excelB64,
//             sheetName, subjectColumns[], scans[]}
//   students: parsed rows from the master Excel [{__row, ...cells}]
//   scans:    [{id, studentId, studentName, result, subject, createdAt}]
let sessions = [];        // all known sessions (local ∪ cloud), newest first
let active = null;        // the active session object
let workbook = null;      // live SheetJS workbook of the active session's Excel

function findSession(id){ return sessions.find(s=> s.id === id) || null; }
function sortSessions(){ sessions.sort((a,b)=> (b.updatedAt||"").localeCompare(a.updatedAt||"")); }

async function persistActive(){
  if(!active) return;
  active.updatedAt = nowIso();
  await saveSessionLocal(active);           // instant, durable, offline-safe
  await setActiveIdLocal(active.id);
  cloudSaveSession(active);                  // async best-effort cloud sync
  renderSidebar();
}

// ===========================================================================
// Excel (SheetJS) — master student sheet import/export
// ===========================================================================
// Reads the uploaded workbook, keeps the raw base64 (so the exact original can
// be re-downloaded / restored later) and parses the first sheet into rows.
async function importExcel(file){
  if(typeof XLSX === "undefined"){ alert("Excel library not loaded yet, please retry in a moment."); return; }
  const buf = await file.arrayBuffer();
  const wb = XLSX.read(buf, {type:"array"});
  const sheetName = wb.SheetNames[0];
  const ws = wb.Sheets[sheetName];
  const rows = XLSX.utils.sheet_to_json(ws, {defval:"", raw:false});
  // Detect likely columns for ID / Name and subject (numeric-mark) columns.
  const cols = rows.length ? Object.keys(rows[0]) : [];
  const subjectColumns = cols.filter(c=> /marks?|subject|score|intel|scien|social|math|गुण|विषय/i.test(c));
  active.excelB64 = arrayBufferToB64(buf);
  active.excelName = file.name;
  active.sheetName = sheetName;
  active.students = rows.map((r, i)=> Object.assign({__row: i + 2}, r)); // +2: header + 1-based
  active.subjectColumns = subjectColumns;
  active.excelMeta = {name:file.name, rows: rows.length, columns: cols, sheetName};
  workbook = wb;
  await persistActive();
  await uploadExcelToStorage(file);
  buildStudentIndex();
  renderExcelStatus();
  renderSubjectPicker();
}
function arrayBufferToB64(buf){
  let binary = "";
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  for(let i=0;i<bytes.length;i+=chunk){
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i+chunk));
  }
  return btoa(binary);
}
function b64ToArrayBuffer(b64){
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for(let i=0;i<binary.length;i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}
async function uploadExcelToStorage(file){
  if(!fb.enabled || !fb.storage || !active) return;
  try{
    const ref = fb.storage.ref("sessions/" + active.id + "/master.xlsx");
    await ref.put(file);
  }catch(_){ /* best-effort */ }
}
// Rebuild the live workbook from the stored base64 (used when reopening a
// session) so Download Updated Excel keeps the original structure.
function rehydrateWorkbook(){
  workbook = null;
  if(active && active.excelB64 && typeof XLSX !== "undefined"){
    try{ workbook = XLSX.read(b64ToArrayBuffer(active.excelB64), {type:"array"}); }catch(_){ workbook = null; }
  }
}
// Write a mark into a student's row for the given subject column, in both the
// parsed `students` model and the live workbook, then persist.
function writeMarkToWorkbook(student, column, value){
  if(!student) return;
  student[column] = value;
  if(workbook && active){
    const ws = workbook.Sheets[active.sheetName || workbook.SheetNames[0]];
    if(ws){
      const range = XLSX.utils.decode_range(ws["!ref"]);
      // Find the header cell matching `column` to get its column index.
      let colIdx = -1;
      for(let c=range.s.c;c<=range.e.c;c++){
        const h = ws[XLSX.utils.encode_cell({r:range.s.r, c})];
        if(h && String(h.v).trim() === column){ colIdx = c; break; }
      }
      if(colIdx !== -1 && student.__row){
        const addr = XLSX.utils.encode_cell({r: student.__row - 1, c: colIdx});
        ws[addr] = {t: typeof value === "number" ? "n" : "s", v: value};
      }
    }
  }
}
// Download the (updated) workbook, preserving original data/structure.
function downloadUpdatedExcel(){
  if(!active || !active.students || !active.students.length){
    alert("Upload a Master Excel sheet for this session first."); return;
  }
  if(!workbook) rehydrateWorkbook();
  let wb = workbook;
  if(!wb){
    // No original workbook available: rebuild from the parsed rows.
    wb = XLSX.utils.book_new();
    const clean = active.students.map(r=>{ const o = Object.assign({}, r); delete o.__row; return o; });
    XLSX.utils.book_append_sheet(wb, XLSX.utils.json_to_sheet(clean), active.sheetName || "Students");
  }
  const base = (active.excelName || (active.name + "_marks") || "marks").replace(/\.(xlsx|xls)$/i, "");
  XLSX.writeFile(wb, base + "_updated.xlsx");
}

// ===========================================================================
// Smart bilingual student search (English + Marathi), from the Excel dataset
// ===========================================================================
let studentIndex = [];   // [{id, name, row(ref to students entry)}]
let idColumn = null, nameColumn = null;

// Fixed NMMS निकालपत्रक roster (roll no. + Marathi name), mirroring the
// backend template exactly. Since the master-Excel upload was removed, this is
// the default student database the smart search runs against so teachers can
// still search a student by ID/roll or name (letter-by-letter, English or
// मराठी) without uploading anything.
const NMMS_FIXED_ROSTER = [
  "दिघे सात्विक प्रमोद",
  "कुलकर्णी ओंकार अभिजित",
  "साबळे रुद्र नानासाहेब",
  "दिघे साई अरुण",
  "नालकर आदित्य नरेंद्र",
  "कडू कृष्णा विवेक",
  "दिघे सार्थक विजय",
  "मुसमाडे अथर्व रविंद्र",
  "कडू समर्थ जयराम",
  "सूर्यवंशी प्रसाद पोपट",
  "सरोदे समर्थ मनोज",
  "गागरे प्रणव कैलास",
  "पवार मंथन अंकुश",
  "अनाप मितेश नानासाहेब",
  "अनाप कृष्णा रोहिदास",
  "जोर्वेकर विराज श्रीकांत",
  "शिंदे सार्थक नितीन",
  "जोर्वेकर सार्थक श्रीधर",
  "पठारे मयूर गणेश",
  "अनाप प्रणव राजू",
  "अनाप साईनाथ अशोक",
  "काळे संकेत रविंद्र",
  "प्रधान श्रेयस चंद्रकांत",
  "उपाध्ये कृष्णा विजय",
  "शिंदे निखील गोरक्षनाथ",
  "अनाप चेतन सुरेश",
  "पर्वत सार्थक संदीप",
  "गायकवाड रुदांत संदीप",
  "भोसले सुदर्शन सुनील",
  "शेजवळ दर्शन मधुकर",
  "गोफणे अनिकेत गोरक्ष",
  "जोशी अथर्व सुनील",
  "बलमे सिद्धार्थ सुखदेव",
  "चितळकर प्रसाद लालचंद",
  "सांगळे सुजय गोरक्षनाथ",
];
function buildFixedRosterIndex(){
  studentIndex = NMMS_FIXED_ROSTER.map((name, i)=>{
    const row = {"प. क्र.": i + 1, "विद्यार्थ्याचे नाव": name};
    return {id: String(i + 1), name: name, row};
  });
  idColumn = "प. क्र.";
  nameColumn = "विद्यार्थ्याचे नाव";
}

function detectColumn(cols, patterns){
  for(const p of patterns){
    const hit = cols.find(c => p.test(c));
    if(hit) return hit;
  }
  return null;
}
function buildStudentIndex(){
  studentIndex = [];
  idColumn = nameColumn = null;
  const rows = (active && active.students) || [];
  // No uploaded master Excel for this session → use the fixed NMMS roster so
  // the smart search always has the student list to match against.
  if(!rows.length){ buildFixedRosterIndex(); return; }
  const cols = Object.keys(rows[0]).filter(c=> c !== "__row");
  idColumn = detectColumn(cols, [/roll\s*no|roll|student\s*id|^id$|id\b|आयडी|रोल|क्रमांक/i]) || cols[0];
  nameColumn = detectColumn(cols, [/student\s*name|name|नाव|विद्यार्थ/i]) || cols[1] || cols[0];
  rows.forEach(r=>{
    studentIndex.push({
      id: String(r[idColumn] ?? "").trim(),
      name: String(r[nameColumn] ?? "").trim(),
      row: r,
    });
  });
}
// Partial, case-insensitive match that also works for Marathi/Unicode. Matches
// if the query appears anywhere in the id or name.
function searchStudents(q){
  q = String(q || "").trim().toLowerCase();
  if(!q) return studentIndex.slice(0, 12);
  // Letter-by-letter search: keep any student whose id or name contains what
  // has been typed so far, then rank results so that names/ids that START with
  // the typed letters come first (a natural "type one letter at a time" feel),
  // with the rest sorted alphabetically.
  const matches = studentIndex.filter(s=>{
    const id = s.id.toLowerCase(), name = s.name.toLowerCase();
    return id.includes(q) || name.includes(q);
  });
  const rank = (s)=>{
    const id = s.id.toLowerCase(), name = s.name.toLowerCase();
    if(id.startsWith(q) || name.startsWith(q)) return 0;   // best: prefix match
    // match at the start of any word in the name (e.g. surname/first name)
    if(name.split(/\s+/).some(w=> w.startsWith(q))) return 1;
    return 2;                                              // anywhere else
  };
  matches.sort((a, b)=>{
    const ra = rank(a), rb = rank(b);
    if(ra !== rb) return ra - rb;
    return a.name.localeCompare(b.name, "mr");
  });
  return matches.slice(0, 25);
}

let selectedStudent = null;   // the student row chosen from the search
let lastResult = null;        // last scored result (set via OMR.onScored)

// Wire an input + its dropdown to the shared student search.
function attachSearch(inputId, dropId){
  const input = document.getElementById(inputId);
  const drop = document.getElementById(dropId);
  if(!input || !drop) return;
  function close(){ drop.style.display = "none"; }
  function open(list){
    if(!studentIndex.length){ close(); return; }
    drop.innerHTML = list.map(s=>
      `<div class="ac-item" data-id="${esc(s.id)}">
         <span class="ac-id">${esc(s.id)}</span>
         <span class="ac-name">${esc(s.name)}</span>
       </div>`).join("") || `<div class="ac-empty">No matching student</div>`;
    drop.style.display = "block";
  }
  input.addEventListener("input", ()=> open(searchStudents(input.value)));
  input.addEventListener("focus", ()=> open(searchStudents(input.value)));
  input.addEventListener("blur", ()=> setTimeout(close, 180)); // allow click
  drop.addEventListener("mousedown", (e)=>{
    const item = e.target.closest(".ac-item");
    if(!item) return;
    const s = studentIndex.find(x=> x.id === item.dataset.id);
    if(s) pickStudent(s);
    close();
  });
}
function pickStudent(s){
  selectedStudent = s;
  const idEl = document.getElementById("studentId");
  const nameEl = document.getElementById("studentName");
  if(idEl) idEl.value = s.id;
  if(nameEl) nameEl.value = s.name;
  // Remember the selected student on the session so it is restored on reopen.
  if(active){ active.selectedStudentId = s.id; persistActive(); }
  // If a fresh score is already available, assign it right away.
  if(lastResult) assignMarks();
}
// Re-select the student that was active when the session was last used, so the
// teacher reopens exactly where they left off.
function restoreSelectedStudent(){
  selectedStudent = null;
  if(!active || !active.selectedStudentId) return;
  const s = studentIndex.find(x=> x.id === String(active.selectedStudentId));
  if(!s) return;
  selectedStudent = s;
  const idEl = document.getElementById("studentId");
  const nameEl = document.getElementById("studentName");
  if(idEl) idEl.value = s.id;
  if(nameEl) nameEl.value = s.name;
}

// ===========================================================================
// Marks assignment: write the scored total into the student's subject column.
// ===========================================================================
function currentSubjectColumn(){
  const sel = document.getElementById("subjectColumnSelect");
  if(sel && sel.value) return sel.value;
  const cols = (active && active.subjectColumns) || [];
  return cols[0] || "Marks";
}
function assignMarks(){
  if(!active){ return; }
  if(!lastResult){ alert("Scan a sheet first, then select the student."); return; }
  if(!selectedStudent){ alert("Select a student from the search suggestions first."); return; }
  const column = currentSubjectColumn();
  const value = lastResult.total_marks;
  const existing = selectedStudent.row[column];
  const hasExisting = existing !== "" && existing != null;
  if(hasExisting && String(existing) !== String(value)){
    if(!confirm(`"${selectedStudent.name || selectedStudent.id}" already has ${existing} in "${column}". Overwrite with ${value}?`)){
      return;
    }
  }
  // Optimistic: update UI/model instantly.
  writeMarkToWorkbook(selectedStudent.row, column, Number(value));
  // One scan = full NMMS result: keep all four subject marks + एकूण total so
  // they survive refresh/close and can auto-fill the निकालपत्रक Excel.
  const subjects = lastResult.subjects || {};
  const total = (lastResult.total != null) ? lastResult.total : value;
  const scan = {
    id: uid(),
    studentId: selectedStudent.id,
    studentName: selectedStudent.name,
    subject: column,
    subjects: subjects,
    total: total,
    marks: value,
    maxMarks: lastResult.max_marks,
    percentage: lastResult.percentage,
    result: lastResult,
    createdAt: nowIso(),
  };
  active.scans = active.scans || [];
  // Replace any earlier scan for this student so the latest result wins.
  active.scans = active.scans.filter(x=> String(x.studentId) !== String(scan.studentId));
  active.scans.push(scan);
  persistActive();
  cloudSaveScan(active.id, scan);            // incremental cloud write
  // Reset the search fields so the teacher can scan the next sheet immediately.
  selectedStudent = null;
  lastResult = null;
  if(active) active.selectedStudentId = null;
  const idEl = document.getElementById("studentId");
  const nameEl = document.getElementById("studentName");
  if(idEl) idEl.value = "";
  if(nameEl) nameEl.value = "";
  const info = document.getElementById("assignInfo");
  if(info){
    const parts = Object.keys(subjects).map(k=> `${k} ${subjects[k]}`).join(" · ");
    info.textContent = `Saved for "${scan.studentName || scan.studentId}" — ${parts ? parts + " · " : ""}एकूण ${total}.`;
  }
  renderSubjectPreview(subjects, total);
  renderExcelStatus();
}
// Show the four subject marks + एकूण total for the last scan in the scan panel.
function renderSubjectPreview(subjects, total){
  const el = document.getElementById("subjectPreview");
  if(!el) return;
  subjects = subjects || {};
  const keys = Object.keys(subjects);
  if(!keys.length){ el.innerHTML = ""; el.style.display = "none"; return; }
  el.style.display = "grid";
  el.innerHTML = keys.map(k=>
    `<div class="subj-cell"><span class="subj-name">${esc(k)}</span><span class="subj-mark">${esc(subjects[k])}</span></div>`
  ).join("") +
    `<div class="subj-cell subj-total"><span class="subj-name">एकूण</span><span class="subj-mark">${esc(total)}</span></div>`;
}

// ===========================================================================
// UI rendering: sidebar, excel status, subject picker
// ===========================================================================
function renderSidebar(){
  const list = document.getElementById("sessionList");
  if(!list) return;
  sortSessions();
  list.innerHTML = sessions.map(s=>{
    const isActive = active && s.id === active.id;
    const cnt = (s.scans || []).length;
    return `<div class="sess-item ${isActive ? "active" : ""}" data-id="${esc(s.id)}">
      <div class="sess-main">
        <div class="sess-name">${esc(s.name)}</div>
        <div class="sess-meta">${cnt} scan${cnt===1?"":"s"} · ${new Date(s.updatedAt||s.createdAt).toLocaleString()}</div>
      </div>
      <button class="sess-rename" data-rename="${esc(s.id)}" title="Rename">✏️</button>
    </div>`;
  }).join("") || `<div class="sess-empty">No sessions yet.</div>`;
}
function renderExcelStatus(){
  const el = document.getElementById("excelStatus");
  if(!el) return;
  if(active && active.excelMeta){
    const m = active.excelMeta;
    el.innerHTML = `📗 <b>${esc(m.name)}</b> · ${m.rows} students · ${(active.scans||[]).length} marked`;
  }else{
    el.textContent = "No master Excel uploaded for this session yet.";
  }
}
function renderSubjectPicker(){
  const sel = document.getElementById("subjectColumnSelect");
  if(!sel) return;
  const cols = (active && active.subjectColumns) || [];
  sel.innerHTML = cols.map(c=> `<option value="${esc(c)}">${esc(c)}</option>`).join("")
    || `<option value="Marks">Marks</option>`;
}

// ===========================================================================
// Session lifecycle
// ===========================================================================
async function newSession(){
  const name = prompt("Name this new scan session (e.g. \"Mid Term Maths\", \"Unit Test Batch A\"):", "");
  if(name === null) return;                          // cancelled
  if(!confirm("Create a new scan session? Your current session stays saved and can be reopened anytime.")) return;
  const sess = {
    id: uid(), name: (name || "Untitled session").trim(),
    createdAt: nowIso(), updatedAt: nowIso(),
    students: [], subjectColumns: [], scans: [],
  };
  sessions.unshift(sess);
  await openSession(sess.id);
}
async function renameSession(id){
  const s = findSession(id);
  if(!s) return;
  const name = prompt("Rename session:", s.name);
  if(name === null || !name.trim()) return;
  s.name = name.trim();
  s.updatedAt = nowIso();
  await saveSessionLocal(s);
  cloudSaveSession(s);
  renderSidebar();
}
async function openSession(id){
  const s = findSession(id);
  if(!s) return;
  active = s;
  active.scans = active.scans || [];
  await setActiveIdLocal(id);
  rehydrateWorkbook();
  buildStudentIndex();
  lastResult = null;
  restoreSelectedStudent();
  renderSidebar();
  renderExcelStatus();
  renderSubjectPicker();
  const title = document.getElementById("activeSessionName");
  if(title) title.textContent = active.name;
  closeSidebar();
}
function openSidebar(){ const s = document.getElementById("sessionSidebar"); if(s) s.classList.add("open"); const b=document.getElementById("sidebarBackdrop"); if(b) b.classList.add("show"); }
function closeSidebar(){ const s = document.getElementById("sessionSidebar"); if(s) s.classList.remove("open"); const b=document.getElementById("sidebarBackdrop"); if(b) b.classList.remove("show"); }

// ===========================================================================
// Bootstrap
// ===========================================================================
async function boot(){
  initFirebase();
  _syncEl = document.getElementById("syncState");

  // Merge local + cloud sessions (local wins on id clash for freshest edits).
  const local = await loadAllSessionsLocal();
  const cloud = await cloudLoadSessions();
  const byId = {};
  cloud.forEach(s=> byId[s.id] = s);
  local.forEach(s=> byId[s.id] = s);      // local overrides cloud
  sessions = Object.values(byId);
  sortSessions();

  // Auto-restore the last active session, or create a first one.
  let activeId = await getActiveIdLocal();
  if(!findSession(activeId)) activeId = sessions.length ? sessions[0].id : null;
  if(activeId){
    await openSession(activeId);
  }else{
    const sess = {id: uid(), name: "Session 1", createdAt: nowIso(), updatedAt: nowIso(),
      students: [], subjectColumns: [], scans: []};
    sessions.unshift(sess);
    await saveSessionLocal(sess);
    await openSession(sess.id);
  }

  // Wire UI controls (guarded: elements exist only if index.html adds them).
  const on = (id, ev, fn)=>{ const el = document.getElementById(id); if(el) el.addEventListener(ev, fn); };
  on("newSessionBtn", "click", newSession);
  on("openSidebarBtn", "click", openSidebar);
  on("closeSidebarBtn", "click", closeSidebar);
  on("sidebarBackdrop", "click", closeSidebar);
  on("downloadExcelBtn", "click", downloadUpdatedExcel);
  const excelInput = document.getElementById("excelInput");
  if(excelInput) excelInput.addEventListener("change", ()=>{
    if(excelInput.files && excelInput.files[0]) importExcel(excelInput.files[0]);
  });
  const list = document.getElementById("sessionList");
  if(list) list.addEventListener("click", (e)=>{
    const rn = e.target.closest("[data-rename]");
    if(rn){ e.stopPropagation(); renameSession(rn.dataset.rename); return; }
    const item = e.target.closest(".sess-item");
    if(item) openSession(item.dataset.id);
  });
  attachSearch("studentId", "studentIdDrop");
  attachSearch("studentName", "studentNameDrop");

  // Sync-state responds to connectivity changes.
  window.addEventListener("online",  ()=> setSync(fb.enabled ? "saved" : "saved"));
  window.addEventListener("offline", ()=> setSync("offline"));
  setSync(navigator.onLine ? "saved" : "offline");
}

// Public hook called by index.html's renderScanResult when a sheet is scored.
OMR.onScored = function(result){
  if(!result || result.scored === false) return;
  lastResult = result;
  // If the teacher already selected a student before scanning, assign now.
  if(selectedStudent) assignMarks();
  else {
    const info = document.getElementById("assignInfo");
    if(info) info.textContent = "Scored " + result.total_marks + "/" + result.max_marks +
      ". Now search & pick the student to save this mark.";
  }
};
OMR.assignMarks = assignMarks;
OMR.downloadUpdatedExcel = downloadUpdatedExcel;

// Collected per-student results of the active session, shaped for the NMMS
// निकालपत्रक Excel auto-fill endpoint (studentId + subjects{} + एकूण total).
OMR.getSessionResults = function(){
  const scans = (active && active.scans) || [];
  return scans.map(s=> ({
    studentId: s.studentId,
    studentName: s.studentName,
    subjects: s.subjects || {},
    total: (s.total != null) ? s.total : s.marks,
  }));
};

if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

})();

