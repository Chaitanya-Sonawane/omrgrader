# OMR Grader

A working OMR (Optical Mark Recognition) scanner and grader, calibrated for your
NMMS-style answer sheet (40 questions, 2 blocks of 20, 4 options each, circular
bubbles inside a ruled table).

It scans a photographed/uploaded answer sheet, detects which bubble is filled for
each question, compares it against an answer key you enter, and produces per-student
and batch results with Excel/PDF export.

## What's actually in here (and what isn't, yet)

This is a real, tested detection pipeline — not a mockup. I ran it against all 12 of
your sample sheets in `SAMPLESSHEET/` (different students, different pens, different
lighting). On every sheet it located the answer grid and detected 152–161 of the 160
bubbles, reading all 40 answers per sheet (a couple of sheets legitimately show a
blank/multi-mark). It's built for
*this specific template*: 40 Qs, 2 blocks, 4 options, ruled grid. If you use a
differently-shaped sheet later, the column/row counts in `omr_engine.py` need
updating (`n_rows`, `n_cols`).

Included and working:
- Upload image or capture via device camera (browser `getUserMedia`)
- **Real-time auto-capture**: live camera preview with per-frame quality validation
  (sheet visibility, distance, blur, lighting, glare, shadows, perspective, tilt,
  grid alignment), on-screen guidance messages ("Move Closer", "Rotate Left",
  "Hold Steady", ...), a 0–100% confidence bar, and automatic hands-free capture
  once the frame has been stable for 20 consecutive checks with confidence > 98%
- Robust bubble detection (handles tilt, uneven lighting, shadows, thin pens, dashed
  grid lines) — anchors on the bubbles themselves rather than fragile ruled lines
- Multi-metric fill scoring (relative darkness + local threshold) with per-row
  normalization to handle lighting gradients across a photographed page
- Blank / multiple-mark detection with a confidence margin
- Answer key entry (per-question dropdown or bulk paste), optional negative marking
- Scoring: correct/wrong/blank/multiple/not-detected, marks, percentage, grade
- Batch dashboard: highest/lowest/average, pass %, hardest questions
- Color-coded Excel export (per-student sheet + summary) and PDF report

Not built (flagged in the original brief as "bonus" — happy to add any of these next):
stored multi-template management, student database/search, admin login, QR/barcode
ID, cloud storage. Manual "start → capture" is still available as a fallback via the
"Capture photo" button or by unticking "Auto capture when steady".

## Auto-capture: how it works

Every ~66 ms the camera tab posts a downscaled preview frame to
`POST /api/frame-check` (with a per-camera `session_id`). The backend
(`frame_quality.py` → `check_frame_quality()`) validates it in under 30 ms and
returns:

```json
{
  "is_ready": false,
  "confidence": 87.5,
  "messages": ["Move Closer", "Hold Steady"],
  "metrics": {"blur": 412.0, "brightness": 201.3, "tilt": 0.8,
              "coverage": 0.41, "perspective": 0.03, "stability": 12,
              "grid_alignment": {"dx": 0.01, "dy": -0.02}, "contrast": 55.2}
}
```

Validation gates (all must pass): outer table contour + grid detected; the template
recognized by counting bubble circles inside the grid; coverage within distance
bounds and no corner cut off; Laplacian-variance blur check; brightness/contrast
bounds plus glare (blown-highlight fraction) and shadow (quadrant brightness spread)
checks; perspective (opposite-edge skew + aspect ratio); tilt from the validated
grid-contour **box-point edge geometry** (not the unreliable `minAreaRect()` angle);
and grid-center alignment against the camera center. A per-session stability tracker
then requires 20 consecutive stable frames (position/scale/angle/blur), resetting on
any movement or bad frame. When `is_ready` is true the browser captures a full-
resolution frame and posts it to `POST /api/auto-capture`, which runs perspective
correction + deskew, shadow removal (background division), contrast enhancement
(CLAHE), then the normal bubble-detection engine (adaptive thresholding + template
alignment happen inside the engine) and scores it if an answer key is set.

### Validation test suite

```bash
cd backend
python test_auto_capture.py   # writes validation_report.md
```

It generates a synthetic sheet with a known key and asserts correct accept/reject
behaviour for: perfect image, 10°/20° rotation, partial crop, low/bright light,
shadow, reflection, motion/Gaussian blur, perspective distortion, too far/close,
off-center, no sheet, and four camera resolutions (640×480 → 1920×1080 + portrait);
plus the 20-frame stability gate, end-to-end bubble accuracy (100% on all pipeline
cases), and the <30 ms / <2 s performance budgets. Current status: **30/30 passing**
(see `backend/validation_report.md`).

## Run it

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser. On a phone, the camera tab needs HTTPS
(or `localhost`) to get camera permission — for real device testing over your LAN,
put it behind a tunnel (ngrok / cloudflared) or serve over HTTPS.

## How to use it

1. **Answer Key tab** — set each question's correct option (1–4), or paste bulk
   text like `1 2\n2 4\n3 1...`. Set marks-per-correct / negative marking if needed.
   Save.
2. **Scan Sheet tab** — upload a photo or capture with the camera, optionally add a
   student ID/name, hit "Scan & score this sheet". You'll see the score plus a
   question-by-question breakdown color-coded correct/wrong/blank.
3. **Results tab** — running dashboard across every sheet scanned this session,
   with Excel/PDF export.

## How the detection actually works

Real phone photos of this sheet have shadows, slight tilt, and — I found this
scanning your samples — **dashed/faint internal grid lines** that break naive
line-detection. So instead of relying on every ruled line being visible:

1. Locate the outer ruled table (robust — this line is always solid).
2. Detect every bubble *circle* inside it directly via contour + circularity
   filtering (bubbles are high-contrast and always present, filled or not).
3. Cluster the ~160 detected circles into a canonical 20-row × 8-column grid with
   k-means — this is what makes it tolerant of missing/dashed lines, since it
   anchors on the marks themselves, not the ruling.
4. Score each bubble's fill using darkness relative to its own row (corrects for
   lighting gradients across the page) combined with a local dark-pixel ratio.
5. Resolve to an answer per question, flagging blanks and ambiguous multi-marks.

## Project structure

```
backend/
  app.py               FastAPI app + endpoints
  omr_engine.py        core CV pipeline (detection, clustering, fill scoring)
  frame_quality.py     real-time frame validation + auto-capture processing
  scoring.py           answer-key comparison, per-student + batch results
  export.py            Excel (color-coded) + PDF report generation
  test_auto_capture.py automated validation suite (writes validation_report.md)
frontend/
  index.html           single-page UI (upload, live auto-capture camera, answer key, results)
```
