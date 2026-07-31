"""Excel (color-coded) and PDF report generation."""
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

GREEN = "C6EFCE"
RED = "FFC7CE"
YELLOW = "FFEB9C"
GREY = "D9D9D9"

STATUS_FILL = {
    "correct": PatternFill(start_color=GREEN, end_color=GREEN, fill_type="solid"),
    "wrong": PatternFill(start_color=RED, end_color=RED, fill_type="solid"),
    "blank": PatternFill(start_color=YELLOW, end_color=YELLOW, fill_type="solid"),
    "multiple": PatternFill(start_color=YELLOW, end_color=YELLOW, fill_type="solid"),
    "not_detected": PatternFill(start_color=GREY, end_color=GREY, fill_type="solid"),
}


def build_excel_report(student_results: list) -> bytes:
    """student_results: list[scoring.StudentResult]"""
    wb = Workbook()

    # --- Summary sheet ---
    ws = wb.active
    ws.title = "Summary"
    headers = ["Student ID", "Name", "Correct", "Wrong", "Blank", "Multiple",
               "Not Detected", "Total Marks", "Max Marks", "Percentage"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")

    for r in student_results:
        ws.append([r.student_id, r.student_name, r.correct, r.wrong, r.blank,
                   r.multiple, r.not_detected, r.total_marks, r.max_marks, r.percentage])

    for i, w in enumerate([14, 22, 9, 9, 9, 10, 12, 12, 11, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # --- Per-student detail sheets ---
    for r in student_results:
        safe_name = (r.student_name or r.student_id or "Student")[:25]
        sheet_name = "".join(c for c in safe_name if c.isalnum() or c in " _-")[:28] or "Student"
        # avoid duplicate sheet names
        base_name, n = sheet_name, 1
        while sheet_name in wb.sheetnames:
            n += 1
            sheet_name = f"{base_name[:25]}_{n}"
        ws2 = wb.create_sheet(sheet_name)
        ws2.append(["Q#", "Correct Answer", "Student Answer", "Status", "Marks"])
        for cell in ws2[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

        for qr in r.question_results:
            row = [qr.question, qr.correct_answer or "-", qr.student_answer or "-",
                   qr.status.replace("_", " ").title(), qr.marks]
            ws2.append(row)
            fill = STATUS_FILL.get(qr.status)
            if fill:
                for c in ws2[ws2.max_row]:
                    c.fill = fill
        for i, w in enumerate([6, 15, 15, 14, 8], start=1):
            ws2.column_dimensions[get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# NMMS weekly result sheet — fixed template built entirely on the backend.
# The heading, student list, columns and formatting mirror the original PDF
# निकालपत्रक exactly. संवर्ग, the four subject-mark columns and एकूण are left
# blank for the user to fill in later. No merged cells; header is bold; columns
# auto-fit. Output file name: NMMS_Result_Test_4.xlsx
# ---------------------------------------------------------------------------
NMMS_TITLE_LINES = [
    "रयत शिक्षण संस्थेचे",
    "नानासाहेब कडू पाटील विद्यालय, सातराळ",
    "NMMS साप्ताहिक निकालपत्रक",
    "सन २०२६–२७",
    "इयत्ता : ८ वी अ  सराव चाचणी क्र. ४  DATE: २७ जुलै २०२६",
]

NMMS_HEADERS = [
    "प. क्र.", "विद्यार्थ्याचे नाव", "संवर्ग",
    "बुद्धिमत्ता", "विज्ञान", "स.शास्त्र", "गणित", "एकूण",
]

NMMS_STUDENTS = [
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
]

NMMS_FILENAME = "NMMS_Result_Test_4.xlsx"


# the four subject columns in the order they appear in NMMS_HEADERS
NMMS_SUBJECT_ORDER = ["बुद्धिमत्ता", "विज्ञान", "स.शास्त्र", "गणित"]


def _index_results_by_student(results):
    """Build a lookup from a list of scanned-result dicts, keyed by both roll
    number (str) and normalized student name, so a student's four subject marks
    + एकूण total can be matched to their fixed roster row."""
    by_roll, by_name = {}, {}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        subjects = r.get("subjects") or {}
        total = r.get("total")
        if total is None:
            total = sum(v for v in subjects.values() if isinstance(v, (int, float)))
        entry = {"subjects": subjects, "total": total}
        rid = r.get("studentId")
        if rid not in (None, ""):
            by_roll[str(rid).strip()] = entry
        name = r.get("studentName")
        if name:
            by_name[str(name).strip()] = entry
    return by_roll, by_name


def build_nmms_result_template(results=None) -> bytes:
    """Build the NMMS weekly result sheet exactly as the original PDF.

    Title lines, header row and the fixed roll-number/student-name list are
    always filled in. When ``results`` (a list of scanned-result dicts with
    ``studentId``/``studentName``/``subjects``/``total``) is provided, each
    matching student's four subject cells + एकूण are auto-filled; संवर्ग and any
    unmatched student's marks stay blank for the user to complete. Header is
    bold, columns auto-fit, and there are no merged cells.
    """
    by_roll, by_name = _index_results_by_student(results)
    wb = Workbook()
    ws = wb.active
    ws.title = "NMMS निकालपत्रक"

    ncols = len(NMMS_HEADERS)
    last_col = get_column_letter(ncols)

    # --- title block ---
    # The heading text is written in column A but centered ACROSS the whole
    # table (A..last_col) using "centerContinuous" alignment. This visually
    # centers the title over the table WITHOUT merging any cells and, crucially,
    # without letting the long title text stretch column A out of alignment.
    for line in NMMS_TITLE_LINES:
        ws.append([line])
        row = ws.max_row
        ws[f"A{row}"].value = line
        ws[f"A{row}"].font = Font(bold=True)
        # apply centerContinuous to every cell in the title span so the text
        # from column A is centered across A..last_col
        for c in range(1, ncols + 1):
            ws.cell(row=row, column=c).alignment = Alignment(
                horizontal="centerContinuous", vertical="center"
            )

    ws.append([])  # spacer row

    # --- bold header row ---
    ws.append(NMMS_HEADERS)
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    first_data_row = header_row + 1

    # --- fixed student rows (संवर्ग always blank; marks/एकूण filled if scanned) ---
    for i, name in enumerate(NMMS_STUDENTS, start=1):
        # Match this fixed roster row to a scanned result by roll no. or name.
        match = by_roll.get(str(i)) or by_name.get(name.strip())
        subj_marks = ["", "", "", ""]
        total_val = ""
        if match:
            subjects = match["subjects"]
            subj_marks = [subjects.get(s, "") for s in NMMS_SUBJECT_ORDER]
            total_val = match["total"]
        ws.append([i, name, "", *subj_marks, total_val])
        row = ws.max_row
        # roll no. centered, name left-aligned, the fill-in columns centered
        ws.cell(row=row, column=1).alignment = Alignment(horizontal="center")
        ws.cell(row=row, column=2).alignment = Alignment(horizontal="left")
        for c in range(3, ncols + 1):
            ws.cell(row=row, column=c).alignment = Alignment(horizontal="center")

    last_data_row = ws.max_row

    # --- auto-fit column widths ---
    # IMPORTANT: only the header row and the student data rows are measured.
    # The title rows are intentionally EXCLUDED, otherwise the long heading text
    # sitting in column A would blow up the प. क्र. column and misalign the
    # whole table.
    for c in range(1, ncols + 1):
        letter = get_column_letter(c)
        longest = len(str(NMMS_HEADERS[c - 1]))
        for r in range(first_data_row, last_data_row + 1):
            val = ws.cell(row=r, column=c).value
            if val not in (None, ""):
                longest = max(longest, len(str(val)))
        ws.column_dimensions[letter].width = max(8, longest + 2)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pdf_report(student_results: list, dashboard: dict | None = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=15 * mm)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("OMR Result Report", styles["Title"]))
    story.append(Spacer(1, 8))

    if dashboard:
        dash_lines = [
            f"Students scanned: {dashboard.get('students_scanned')}",
            f"Highest marks: {dashboard.get('highest_marks')}",
            f"Lowest marks: {dashboard.get('lowest_marks')}",
            f"Average marks: {dashboard.get('average_marks')}",
            f"Pass %: {dashboard.get('pass_percent')}  |  Fail %: {dashboard.get('fail_percent')}",
        ]
        for line in dash_lines:
            story.append(Paragraph(line, styles["Normal"]))
        story.append(Spacer(1, 12))

    table_data = [["Student ID", "Name", "Correct", "Wrong", "Blank", "Total", "Max", "%"]]
    for r in student_results:
        table_data.append([r.student_id, r.student_name, r.correct, r.wrong, r.blank,
                            r.total_marks, r.max_marks, f"{r.percentage}%"])
    t = Table(table_data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
    ]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()
