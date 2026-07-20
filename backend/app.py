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
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from omr_engine import scan_omr_sheet, BubbleResult
from frame_quality import check_frame_quality, process_captured_image, reset_tracker
from scoring import score_sheet, batch_dashboard, grade_for_percentage, StudentResult
from export import build_excel_report, build_pdf_report

app = FastAPI(title="OMR Scanner & Grader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- in-memory session state (swap for a DB in production) ----
STATE = {
    "answer_key": {},       # {1: 2, 2: 4, ...}
    "marks_per_correct": 1.0,
    "negative_marking": 0.0,
    "results": [],          # list[StudentResult]
}


class AnswerKeyIn(BaseModel):
    answers: dict[str, int]           # {"1": 2, "2": 4, ...}
    marks_per_correct: float = 1.0
    negative_marking: float = 0.0


@app.post("/api/answer-key")
def set_answer_key(payload: AnswerKeyIn):
    STATE["answer_key"] = {int(k): v for k, v in payload.answers.items()}
    STATE["marks_per_correct"] = payload.marks_per_correct
    STATE["negative_marking"] = payload.negative_marking
    return {"ok": True, "questions": len(STATE["answer_key"])}


@app.get("/api/answer-key")
def get_answer_key():
    return {
        "answers": STATE["answer_key"],
        "marks_per_correct": STATE["marks_per_correct"],
        "negative_marking": STATE["negative_marking"],
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
    if STATE["answer_key"]:
        student_result = score_sheet(
            scan_result.answers, STATE["answer_key"],
            marks_per_correct=STATE["marks_per_correct"],
            negative_marking=STATE["negative_marking"],
            student_id=student_id, student_name=student_name,
        )
        student_result.quality_warnings = scan_result.warnings
        STATE["results"].append(student_result)
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
    file: UploadFile = File(...),
    student_id: str = Form(""),
    student_name: str = Form(""),
):
    if not STATE["answer_key"]:
        raise HTTPException(400, "No answer key set. POST /api/answer-key first.")
    content = await file.read()
    try:
        scan_result = scan_omr_sheet(content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    student_result = score_sheet(
        scan_result.answers, STATE["answer_key"],
        marks_per_correct=STATE["marks_per_correct"],
        negative_marking=STATE["negative_marking"],
        student_id=student_id, student_name=student_name,
    )
    student_result.quality_warnings = scan_result.warnings
    STATE["results"].append(student_result)

    out = _student_result_to_dict(student_result)
    out["quality"] = scan_result.quality
    out["warnings"] = scan_result.warnings
    return out


@app.post("/api/batch-scan-and-score")
async def batch_scan_and_score(files: list[UploadFile] = File(...)):
    if not STATE["answer_key"]:
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
            scan_result.answers, STATE["answer_key"],
            marks_per_correct=STATE["marks_per_correct"],
            negative_marking=STATE["negative_marking"],
            student_id=f.filename, student_name="",
        )
        student_result.quality_warnings = scan_result.warnings
        STATE["results"].append(student_result)
        d = _student_result_to_dict(student_result)
        d["filename"] = f.filename
        d["warnings"] = scan_result.warnings
        out.append(d)

    dash = batch_dashboard(STATE["results"])
    return {"results": out, "dashboard": dash}


@app.get("/api/results")
def get_results():
    dash = batch_dashboard(STATE["results"]) if STATE["results"] else {}
    return {
        "results": [_student_result_to_dict(r) for r in STATE["results"]],
        "dashboard": dash,
    }


@app.delete("/api/results")
def clear_results():
    STATE["results"] = []
    return {"ok": True}


@app.get("/api/export/excel")
def export_excel():
    if not STATE["results"]:
        raise HTTPException(400, "No results to export yet.")
    data = build_excel_report(STATE["results"])
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=omr_results.xlsx"},
    )


@app.get("/api/export/pdf")
def export_pdf():
    if not STATE["results"]:
        raise HTTPException(400, "No results to export yet.")
    dash = batch_dashboard(STATE["results"])
    data = build_pdf_report(STATE["results"], dash)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=omr_results.pdf"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


# serve the frontend (single-page app) at /
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
