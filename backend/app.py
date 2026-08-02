"""
OMR Scanner & Grader - FastAPI backend.

Endpoints:
  POST /api/answer-key          save the answer key (JSON: {"1":"2", "2":"4", ...})
  GET  /api/answer-key          fetch current answer key
  POST /api/frame-check         validate ONE live preview frame (auto-capture gating)
  POST /api/frame-check/reset   reset the stability tracker for a camera session
  POST /api/auto-capture        process an auto-captured frame + scan (+ score if key set)
  POST /api/scan                scan ONE sheet, return detected answers + quality info
  POST /api/scan-and-score      scan + score against saved answer key -> StudentResult
  POST /api/batch-scan-and-score  scan multiple sheets in one call -> list + dashboard
  GET  /api/export/excel        export all scored results (session) as .xlsx
  GET  /api/export/pdf          export all scored results (session) as .pdf
  GET  /api/results             list all scored results in this session
  DELETE /api/results           clear session results
"""
import io
import json
import os
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from omr_engine import scan_omr_sheet, BubbleResult
from frame_quality import check_frame_quality, process_captured_image, reset_tracker
from scoring import (
    score_sheet, batch_dashboard, grade_for_percentage, StudentResult,
    subject_breakdown, NMMS_SUBJECT_RANGES,
)
from export import build_excel_report, build_pdf_report, build_nmms_result_template, NMMS_FILENAME

app = FastAPI(title="OMR Scanner & Grader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- per-client in-memory state (swap for a DB in production) ----
# Every user/device gets its OWN isolated bucket of answer key + results, so
# two people using the app at the same time never share or overwrite each
# other's data. The bucket is keyed by the client identifier the frontend
# sends (`X-Client-Id`, one stable id per browser) and falls back to the
# caller's IP address when no such header is present.
def _new_state():
    return {
        "answer_key": {},       # {1: 2, 2: 4, ...}
        "marks_per_correct": 1.0,
        "negative_marking": 0.0,
        "results": [],          # list[StudentResult]
    }


_STATES: dict[str, dict] = {}


def _client_key(request: Request) -> str:
    """Stable per-user key: prefer the browser's X-Client-Id, else the IP."""
    cid = request.headers.get("x-client-id")
    if cid and cid.strip():
        return "cid:" + cid.strip()
    # Behind a proxy (Render/Netlify) the real client IP is in X-Forwarded-For.
    xff = request.headers.get("x-forwarded-for")
    if xff and xff.strip():
        return "ip:" + xff.split(",")[0].strip()
    return "ip:" + (request.client.host if request.client else "unknown")


def get_state(request: Request) -> dict:
    key = _client_key(request)
    state = _STATES.get(key)
    if state is None:
        state = _new_state()
        _STATES[key] = state
    return state


class AnswerKeyIn(BaseModel):
    answers: dict[str, int]           # {"1": 2, "2": 4, ...}
    marks_per_correct: float = 1.0
    negative_marking: float = 0.0


@app.post("/api/answer-key")
def set_answer_key(payload: AnswerKeyIn, request: Request):
    state = get_state(request)
    state["answer_key"] = {int(k): v for k, v in payload.answers.items()}
    state["marks_per_correct"] = payload.marks_per_correct
    state["negative_marking"] = payload.negative_marking
    return {"ok": True, "questions": len(state["answer_key"])}


@app.get("/api/answer-key")
def get_answer_key(request: Request):
    state = get_state(request)
    return {
        "answers": state["answer_key"],
        "marks_per_correct": state["marks_per_correct"],
        "negative_marking": state["negative_marking"],
    }


def _bubble_to_dict(b: BubbleResult):
    return {
        "question": b.question,
        "selected": b.selected,
        "is_multiple": b.is_multiple,
        "multiple_options": b.multiple_options,
        "confidences": {str(k): v for k, v in b.confidences.items()},
        "flagged": b.flagged,
        "flag_reason": b.flag_reason,
    }


def _student_result_to_dict(r: StudentResult):
    return {
        "student_id": r.student_id,
        "student_name": r.student_name,
        "total_questions": r.total_questions,
        "correct": r.correct,
        "wrong": r.wrong,
        "blank": r.blank,
        "multiple": r.multiple,
        "not_detected": r.not_detected,
        "total_marks": r.total_marks,
        "max_marks": r.max_marks,
        "percentage": r.percentage,
        "grade": grade_for_percentage(r.percentage),
        "question_results": [
            {"question": q.question, "correct_answer": q.correct_answer,
             "student_answer": q.student_answer, "status": q.status, "marks": q.marks}
            for q in r.question_results
        ],
        # NMMS subject split (four subjects + एकूण) from this single scan.
        **subject_breakdown(r),
    }


@app.post("/api/frame-check")
async def frame_check(file: UploadFile = File(...), session_id: str = Form("default")):
    """Validate a single live camera frame. Returns is_ready/confidence/messages/metrics.
    Stability is tracked per session_id across consecutive calls."""
    content = await file.read()
    try:
        return check_frame_quality(content, session_id=session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/frame-check/reset")
def frame_check_reset(session_id: str = Form("default")):
    """Reset the stability counter (call when the camera starts/stops)."""
    reset_tracker(session_id)
    return {"ok": True}


@app.post("/api/auto-capture")
async def auto_capture(
    request: Request,
    file: UploadFile = File(...),
    student_id: str = Form(""),
    student_name: str = Form(""),
):
    """Process an auto-captured frame (perspective correction, deskew, shadow
    removal, contrast enhancement), then scan it; scores too if a key is set."""
    content = await file.read()
    try:
        processed = process_captured_image(content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # scan the processed image; fall back to the raw frame if the cleaned
    # version somehow fails (belt and braces - it passed 20 quality gates)
    try:
        scan_result = scan_omr_sheet(processed)
    except ValueError:
        try:
            scan_result = scan_omr_sheet(content)
        except ValueError as e:
            raise HTTPException(400, str(e))

    out = {
        "answers": {str(q): _bubble_to_dict(b) for q, b in scan_result.answers.items()},
        "quality": scan_result.quality,
        "warnings": scan_result.warnings,
        "scored": False,
    }
    state = get_state(request)
    if state["answer_key"]:
        student_result = score_sheet(
            scan_result.answers, state["answer_key"],
            marks_per_correct=state["marks_per_correct"],
            negative_marking=state["negative_marking"],
            student_id=student_id, student_name=student_name,
        )
        student_result.quality_warnings = scan_result.warnings
        state["results"].append(student_result)
        out.update(_student_result_to_dict(student_result))
        out["scored"] = True
    return out


@app.post("/api/scan")
async def scan(file: UploadFile = File(...)):
    content = await file.read()
    try:
        result = scan_omr_sheet(content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "answers": {str(q): _bubble_to_dict(b) for q, b in result.answers.items()},
        "quality": result.quality,
        "warnings": result.warnings,
    }


@app.post("/api/scan-and-score")
async def scan_and_score(
    request: Request,
    file: UploadFile = File(...),
    student_id: str = Form(""),
    student_name: str = Form(""),
):
    state = get_state(request)
    if not state["answer_key"]:
        raise HTTPException(400, "No answer key set. POST /api/answer-key first.")
    content = await file.read()
    try:
        scan_result = scan_omr_sheet(content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    student_result = score_sheet(
        scan_result.answers, state["answer_key"],
        marks_per_correct=state["marks_per_correct"],
        negative_marking=state["negative_marking"],
        student_id=student_id, student_name=student_name,
    )
    student_result.quality_warnings = scan_result.warnings
    state["results"].append(student_result)

    out = _student_result_to_dict(student_result)
    out["quality"] = scan_result.quality
    out["warnings"] = scan_result.warnings
    # Questions the engine is unsure about (low confidence / multiple marks /
    # not detected) so the UI can prompt the teacher to double-check them.
    out["flagged_questions"] = [
        {"question": q, "reason": b.flag_reason}
        for q, b in sorted(scan_result.answers.items())
        if b.flagged
    ]
    return out


@app.post("/api/batch-scan-and-score")
async def batch_scan_and_score(request: Request, files: list[UploadFile] = File(...)):
    state = get_state(request)
    if not state["answer_key"]:
        raise HTTPException(400, "No answer key set. POST /api/answer-key first.")
    out = []
    for f in files:
        content = await f.read()
        try:
            scan_result = scan_omr_sheet(content)
        except ValueError as e:
            out.append({"filename": f.filename, "error": str(e)})
            continue
        student_result = score_sheet(
            scan_result.answers, state["answer_key"],
            marks_per_correct=state["marks_per_correct"],
            negative_marking=state["negative_marking"],
            student_id=f.filename, student_name="",
        )
        student_result.quality_warnings = scan_result.warnings
        state["results"].append(student_result)
        d = _student_result_to_dict(student_result)
        d["filename"] = f.filename
        d["warnings"] = scan_result.warnings
        out.append(d)

    dash = batch_dashboard(state["results"])
    return {"results": out, "dashboard": dash}


@app.get("/api/results")
def get_results(request: Request):
    state = get_state(request)
    dash = batch_dashboard(state["results"]) if state["results"] else {}
    return {
        "results": [_student_result_to_dict(r) for r in state["results"]],
        "dashboard": dash,
    }


@app.delete("/api/results")
def clear_results(request: Request):
    state = get_state(request)
    state["results"] = []
    return {"ok": True}


@app.get("/api/export/excel")
def export_excel(request: Request):
    state = get_state(request)
    if not state["results"]:
        raise HTTPException(400, "No results to export yet.")
    data = build_excel_report(state["results"])
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=omr_results.xlsx"},
    )


class NmmsFillIn(BaseModel):
    # per-student scanned results: [{studentId, studentName, subjects{}, total}]
    results: list[dict] = []


def _nmms_streaming_response(data: bytes):
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={NMMS_FILENAME}"},
    )


@app.get("/api/export/nmms-template")
def export_nmms_template():
    """Return the fixed NMMS weekly result sheet (heading + student list) as a
    ready-to-fill .xlsx generated entirely on the backend (marks blank)."""
    return _nmms_streaming_response(build_nmms_result_template())


@app.post("/api/export/nmms-template")
def export_nmms_template_filled(payload: NmmsFillIn):
    """Return the NMMS निकालपत्रक with each matching student's four subject marks
    + एकूण auto-filled from the scanned results sent by the client."""
    return _nmms_streaming_response(build_nmms_result_template(payload.results))


@app.get("/api/export/pdf")
def export_pdf(request: Request):
    state = get_state(request)
    if not state["results"]:
        raise HTTPException(400, "No results to export yet.")
    dash = batch_dashboard(state["results"])
    data = build_pdf_report(state["results"], dash)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=omr_results.pdf"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


# serve the frontend (single-page app) at / when it is bundled alongside the
# backend. Resolve the path relative to this file (not the current working
# directory) and skip the mount if the folder isn't present (e.g. on Render,
# where the frontend is deployed separately on Netlify).
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
