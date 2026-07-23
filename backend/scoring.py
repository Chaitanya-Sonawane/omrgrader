"""Answer key comparison, scoring, and result aggregation."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QuestionResult:
    question: int
    correct_answer: Optional[int]
    student_answer: Optional[int]
    status: str          # "correct" | "wrong" | "blank" | "multiple" | "not_detected"
    marks: float


@dataclass
class StudentResult:
    student_id: str
    student_name: str
    total_questions: int
    correct: int = 0
    wrong: int = 0
    blank: int = 0
    multiple: int = 0
    not_detected: int = 0
    total_marks: float = 0.0
    max_marks: float = 0.0
    percentage: float = 0.0
    question_results: list = field(default_factory=list)
    quality_warnings: list = field(default_factory=list)


def score_sheet(scan_answers: dict, answer_key: dict,
                 marks_per_correct: float = 1.0,
                 negative_marking: float = 0.0,
                 student_id: str = "", student_name: str = "") -> StudentResult:
    """
    scan_answers: dict[int question] -> BubbleResult (from omr_engine)
    answer_key:   dict[int question] -> int correct_option (1-4)

    The sheet is scored over the full set of questions present (the union of
    the answer key and the questions detected on the scanned sheet). This keeps
    the maximum marks tied to the real number of questions on the sheet (e.g.
    40) even if a correct answer for one question was never set in the key,
    instead of silently shrinking the total to the number of saved key entries.
    """
    all_questions = set(answer_key.keys()) | set(scan_answers.keys())
    total_q = max(all_questions) if all_questions else 0
    result = StudentResult(
        student_id=student_id,
        student_name=student_name,
        total_questions=total_q,
        max_marks=total_q * marks_per_correct,
    )

    for q in range(1, total_q + 1):
        correct_opt = answer_key.get(q)
        bubble = scan_answers.get(q)

        if correct_opt is None:
            # No correct answer configured for this question: it still counts
            # toward the total (so the sheet stays out of its full size), but
            # it cannot be graded, so it is treated as blank (0 marks).
            status, marks = "blank", 0.0
            result.blank += 1
        elif bubble is None or (bubble.flagged and bubble.flag_reason == "not detected"):
            status, marks = "not_detected", 0.0
            result.not_detected += 1
        elif bubble.is_multiple:
            status, marks = "multiple", -negative_marking
            result.multiple += 1
        elif bubble.selected is None:
            status, marks = "blank", 0.0
            result.blank += 1
        elif bubble.selected == correct_opt:
            status, marks = "correct", marks_per_correct
            result.correct += 1
        else:
            status, marks = "wrong", -negative_marking
            result.wrong += 1

        result.total_marks += marks
        result.question_results.append(QuestionResult(
            question=q,
            correct_answer=correct_opt,
            student_answer=(bubble.selected if bubble else None),
            status=status,
            marks=marks,
        ))

    result.total_marks = max(0.0, round(result.total_marks, 2))
    result.percentage = round(
        (result.total_marks / result.max_marks * 100) if result.max_marks else 0.0, 2
    )
    return result


def grade_for_percentage(pct: float) -> str:
    if pct >= 90: return "A+"
    if pct >= 80: return "A"
    if pct >= 70: return "B+"
    if pct >= 60: return "B"
    if pct >= 50: return "C"
    if pct >= 40: return "D"
    return "F"


def batch_dashboard(results: list[StudentResult]) -> dict:
    if not results:
        return {}
    marks = [r.total_marks for r in results]
    pass_mark_pct = 40
    passed = sum(1 for r in results if r.percentage >= pass_mark_pct)
    # per-question difficulty: % of students who got it wrong
    q_wrong_count = {}
    q_total = {}
    for r in results:
        for qr in r.question_results:
            q_total[qr.question] = q_total.get(qr.question, 0) + 1
            if qr.status in ("wrong", "blank", "multiple"):
                q_wrong_count[qr.question] = q_wrong_count.get(qr.question, 0) + 1
    difficulty = sorted(
        [(q, round(q_wrong_count.get(q, 0) / q_total[q] * 100, 1)) for q in q_total],
        key=lambda x: -x[1]
    )
    return {
        "students_scanned": len(results),
        "highest_marks": max(marks),
        "lowest_marks": min(marks),
        "average_marks": round(sum(marks) / len(marks), 2),
        "pass_percent": round(passed / len(results) * 100, 1),
        "fail_percent": round(100 - passed / len(results) * 100, 1),
        "hardest_questions": difficulty[:10],
    }
