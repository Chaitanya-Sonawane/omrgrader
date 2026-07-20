"""
Automated validation suite for the OMR auto-capture system.

Generates a synthetic NMMS-style answer sheet (same template the engine is
calibrated for: 20 rows x 2 blocks x 4 options, ruled table), simulates a
range of good/bad camera captures, and asserts that:

  - check_frame_quality() ACCEPTS every good frame
  - check_frame_quality() REJECTS every simulated bad capture with the
    correct guidance message
  - the stability gate only opens after 20 consecutive stable frames and
    resets on movement
  - per-frame validation stays under the 30 ms budget
  - the full auto-capture pipeline (process_captured_image + scan_omr_sheet)
    reads back the marked answers with 100% accuracy on a clean capture

Run:  .venv/bin/python test_auto_capture.py
Writes a detailed markdown report to validation_report.md
"""
import time

import cv2
import numpy as np

from frame_quality import (
    check_frame_quality, process_captured_image, StabilityTracker,
    STABLE_FRAMES_REQUIRED, reset_tracker,
)
from omr_engine import scan_omr_sheet

RNG = np.random.default_rng(42)

# ---------------------------------------------------------------------------
# Synthetic sheet + camera-frame generation
# ---------------------------------------------------------------------------
ANSWERS = {q: ((q * 7) % 4) + 1 for q in range(1, 41)}   # deterministic key


def make_sheet(sheet_w=1400, marked=ANSWERS):
    """Draw a synthetic answer sheet: ruled table, 20 rows x 2 blocks x 4
    options, circular bubbles; `marked` answers filled in."""
    grid_w = int(sheet_w * 0.86)
    grid_h = int(grid_w / 1.35)                  # template aspect ratio
    margin_x = (sheet_w - grid_w) // 2
    margin_y = int(sheet_w * 0.09)
    sheet_h = grid_h + 2 * margin_y
    img = np.full((sheet_h, sheet_w, 3), 245, np.uint8)

    x0, y0 = margin_x, margin_y
    n_rows, n_cols = 21, 10                      # 1 header row; 2x(Qno+4 opts)
    cell_w, cell_h = grid_w / n_cols, grid_h / n_rows

    for r in range(n_rows + 1):                  # horizontal rules
        y = int(y0 + r * cell_h)
        cv2.line(img, (x0, y), (x0 + grid_w, y), (60, 60, 60), 2)
    for c in range(n_cols + 1):                  # vertical rules
        x = int(x0 + c * cell_w)
        cv2.line(img, (x, y0), (x, y0 + grid_h), (60, 60, 60), 2)

    radius = int(min(cell_w, cell_h) * 0.30)
    for row in range(20):
        for block, (col_off, q_off) in enumerate([(1, 0), (6, 20)]):
            q = row + 1 + q_off
            for opt in range(4):
                cx = int(x0 + (col_off + opt + 0.5) * cell_w)
                cy = int(y0 + (row + 1.5) * cell_h)
                cv2.circle(img, (cx, cy), radius, (90, 90, 90), 2)
                if marked.get(q) == opt + 1:
                    cv2.circle(img, (cx, cy), radius - 2, (25, 25, 25), -1)
    return img


def make_frame(sheet=None, frame_size=(960, 720), scale=0.82, shift=(0, 0),
               angle=0.0, bg_level=120):
    """Place the sheet into a simulated camera frame with the given scale,
    center offset (pixels) and rotation (degrees)."""
    if sheet is None:
        sheet = make_sheet()
    fw, fh = frame_size
    frame = np.full((fh, fw, 3), bg_level, np.uint8)

    sh, sw = sheet.shape[:2]
    s = min(fw / sw, fh / sh) * scale
    warp = cv2.resize(sheet, (int(sw * s), int(sh * s)))
    if angle:
        h2, w2 = warp.shape[:2]
        M = cv2.getRotationMatrix2D((w2 / 2, h2 / 2), angle, 1.0)
        cos, sin = abs(M[0, 0]), abs(M[0, 1])
        nw, nh = int(h2 * sin + w2 * cos), int(h2 * cos + w2 * sin)
        M[0, 2] += nw / 2 - w2 / 2
        M[1, 2] += nh / 2 - h2 / 2
        warp = cv2.warpAffine(warp, M, (nw, nh),
                              borderValue=(bg_level, bg_level, bg_level))
    h2, w2 = warp.shape[:2]
    x = (fw - w2) // 2 + shift[0]
    y = (fh - h2) // 2 + shift[1]
    xs0, ys0 = max(0, x), max(0, y)
    xs1, ys1 = min(fw, x + w2), min(fh, y + h2)
    frame[ys0:ys1, xs0:xs1] = warp[ys0 - y:ys1 - y, xs0 - x:xs1 - x]
    return frame


def perspective_frame(strength=0.10, **kw):
    """Apply a keystone perspective distortion to a good frame."""
    frame = make_frame(**kw)
    h, w = frame.shape[:2]
    d = int(w * strength)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[d, 0], [w - d, 0], [w, h], [0, h]])
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(frame, M, (w, h), borderValue=(120, 120, 120))


def add_shadow(frame):
    out = frame.astype(np.float32)
    h, w = out.shape[:2]
    ramp = np.linspace(1.0, 0.35, w)[None, :, None]
    return np.clip(out * ramp, 0, 255).astype(np.uint8)


def add_reflection(frame):
    """Soft flash glare: brighten a blurred spot without blurring the sheet."""
    h, w = frame.shape[:2]
    spot = np.zeros((h, w), np.uint8)
    cv2.circle(spot, (w // 2, h // 3), int(min(h, w) * 0.12), 255, -1)
    spot = cv2.GaussianBlur(spot, (41, 41), 0).astype(np.float32)
    out = frame.astype(np.float32) + spot[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def check(frame, session="test", stability=False):
    return check_frame_quality(frame, session_id=session,
                               track_stability=stability)


# ---------------------------------------------------------------------------
# Test cases: (name, frame factory, expect_pass, expected message substrings)
# ---------------------------------------------------------------------------
def build_cases():
    sheet = make_sheet()
    return [
        ("Perfect image", lambda: make_frame(sheet), True, []),
        ("10 deg rotation", lambda: make_frame(sheet, angle=10, scale=0.72),
         False, ["Rotate"]),
        ("20 deg rotation", lambda: make_frame(sheet, angle=20, scale=0.62),
         False, ["Rotate"]),
        ("Partial crop", lambda: make_frame(sheet, scale=0.9, shift=(260, 0)),
         False, ["Sheet Cropped", "Move"]),
        ("Low light", lambda: np.clip(
            make_frame(sheet).astype(np.int16) - 185, 0, 255).astype(np.uint8),
         False, ["Too Dark"]),
        ("Bright light", lambda: np.clip(
            make_frame(sheet).astype(np.int16) + 105, 0, 255).astype(np.uint8),
         False, ["Too Bright"]),
        ("Shadow", lambda: add_shadow(make_frame(sheet)), False,
         ["Shadow Detected"]),
        ("Reflection", lambda: add_reflection(make_frame(sheet)), False,
         ["Reflection Detected"]),
        ("Motion blur", lambda: cv2.filter2D(
            make_frame(sheet), -1, np.ones((1, 25), np.float32) / 25), False,
         ["Image Blurry", "Align Sheet", "Entire Sheet Not Visible"]),
        ("Gaussian blur", lambda: cv2.GaussianBlur(
            make_frame(sheet), (25, 25), 8), False, ["Image Blurry"]),
        ("Perspective distortion", lambda: perspective_frame(0.12), False,
         ["Align Sheet"]),
        ("Too far", lambda: make_frame(sheet, scale=0.30), False,
         ["Move Closer"]),
        ("Too close", lambda: make_frame(sheet, scale=1.35), False,
         ["Sheet Cropped", "Move Away"]),
        ("Off-center left", lambda: make_frame(sheet, scale=0.68,
                                               shift=(-140, 0)),
         False, ["Move Right"]),
        ("Off-center down", lambda: make_frame(sheet, scale=0.68,
                                               shift=(0, 80)),
         False, ["Move Up"]),
        ("No sheet", lambda: np.full((720, 960, 3), 120, np.uint8), False,
         ["Entire Sheet Not Visible"]),
        # different camera resolutions / phones
        ("Low-res phone 640x480", lambda: make_frame(sheet, (640, 480)),
         True, []),
        ("HD phone 1280x720", lambda: make_frame(sheet, (1280, 720)),
         True, []),
        ("Flagship 1920x1080", lambda: make_frame(sheet, (1920, 1080)),
         True, []),
        ("Portrait phone 720x1280", lambda: make_frame(sheet, (720, 1280),
                                                       scale=0.85),
         True, []),
    ]


def run_frame_tests(report):
    cases = build_cases()
    passed = 0
    report.append("\n## Frame validation tests\n")
    report.append("| Test | Expected | Result | Confidence | Messages | Time (ms) |")
    report.append("|---|---|---|---|---|---|")
    failures = []
    for name, factory, expect_pass, expect_msgs in cases:
        frame = factory()
        t0 = time.perf_counter()
        res = check(frame)
        dt = (time.perf_counter() - t0) * 1000
        # "frame passes" = no hard rejection (stability handled separately)
        frame_ok = res["confidence"] >= 90.0 and not any(
            m for m in res["messages"]
            if m not in ("Hold Steady", "Ready to Capture"))
        ok = frame_ok == expect_pass
        if ok and not expect_pass and expect_msgs:
            ok = any(any(sub in m for m in res["messages"])
                     for sub in expect_msgs)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failures.append((name, res))
        report.append(
            f"| {name} | {'accept' if expect_pass else 'reject'} "
            f"| {status} | {res['confidence']}% "
            f"| {'; '.join(res['messages']) or '-'} | {dt:.1f} |")
    report.append(f"\n**{passed}/{len(cases)} frame tests passed.**")
    return passed, len(cases), failures


def run_stability_tests(report):
    """20 stable frames required; movement resets the counter."""
    report.append("\n## Stability gate tests\n")
    sheet = make_sheet()
    good = make_frame(sheet)
    session = "stab-test"
    reset_tracker(session)

    results = []
    ready_at = None
    for i in range(STABLE_FRAMES_REQUIRED + 3):
        r = check_frame_quality(good, session_id=session)
        results.append(r)
        if r["is_ready"] and ready_at is None:
            ready_at = i
    t1 = (ready_at == STABLE_FRAMES_REQUIRED,
          f"is_ready first True on frame {ready_at} "
          f"(expected {STABLE_FRAMES_REQUIRED})")

    # movement must reset the counter
    moved = make_frame(sheet, shift=(60, 0))
    r_moved = check_frame_quality(moved, session_id=session)
    t2 = (r_moved["metrics"]["stability"] == 0 and not r_moved["is_ready"],
          f"stability after movement = {r_moved['metrics']['stability']} "
          "(expected 0)")

    # a bad frame must also reset
    reset_tracker(session)
    for _ in range(10):
        check_frame_quality(good, session_id=session)
    blurry = cv2.GaussianBlur(good, (25, 25), 8)
    r_bad = check_frame_quality(blurry, session_id=session)
    t3 = (r_bad["metrics"]["stability"] == 0,
          f"stability after bad frame = {r_bad['metrics']['stability']} "
          "(expected 0)")

    # confidence at readiness must exceed 98%
    conf = results[ready_at]["confidence"] if ready_at is not None else 0
    t4 = (conf > 98.0, f"confidence at capture = {conf}% (required > 98%)")

    passed = 0
    for ok, desc in (t1, t2, t3, t4):
        report.append(f"- {'PASS' if ok else 'FAIL'}: {desc}")
        passed += ok
    return passed, 4


def run_pipeline_tests(report):
    """End-to-end: auto-captured frame -> processing -> bubble detection."""
    report.append("\n## Auto-capture pipeline tests\n")
    total, passed = 0, 0

    for name, kwargs in [
        ("clean capture", {}),
        ("slight tilt (2 deg)", {"angle": 2, "scale": 0.78}),
        ("close distance", {"scale": 0.9}),
        ("far distance", {"scale": 0.6}),
    ]:
        frame = make_frame(make_sheet(), frame_size=(1600, 1200), **kwargs)
        ok_enc, buf = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 95])
        raw = buf.tobytes()
        total += 1
        try:
            t0 = time.perf_counter()
            processed = process_captured_image(raw)
            result = scan_omr_sheet(processed)
            dt = time.perf_counter() - t0
            detected = {q: b.selected for q, b in result.answers.items()}
            correct = sum(detected.get(q) == a for q, a in ANSWERS.items())
            acc = 100.0 * correct / len(ANSWERS)
            ok = acc >= 99.9
            passed += ok
            report.append(
                f"- {'PASS' if ok else 'FAIL'}: {name} - bubble accuracy "
                f"{acc:.1f}% ({correct}/40), processed in {dt:.2f}s")
        except ValueError as e:
            report.append(f"- FAIL: {name} - pipeline error: {e}")
    return passed, total


def run_performance_tests(report):
    report.append("\n## Performance tests\n")
    frame = make_frame(make_sheet())
    check(frame)  # warm-up
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        check(frame)
        times.append((time.perf_counter() - t0) * 1000)
    avg, p95 = float(np.mean(times)), float(np.percentile(times, 95))
    ok = avg < 30.0
    report.append(f"- {'PASS' if ok else 'FAIL'}: frame validation avg "
                  f"{avg:.1f} ms, p95 {p95:.1f} ms (budget 30 ms)")
    # 20 stable frames at 15 checks/sec ~= 1.33s -> auto capture < 2s
    capture_time = STABLE_FRAMES_REQUIRED * (1000 / 15 + avg) / 1000
    ok2 = capture_time < 2.0
    report.append(f"- {'PASS' if ok2 else 'FAIL'}: estimated time-to-capture "
                  f"once steady = {capture_time:.2f}s (budget 2s)")
    return int(ok) + int(ok2), 2


def main():
    report = ["# OMR Auto-Capture Validation Report",
              f"\nGenerated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"]
    totals = [0, 0]

    for fn in (run_frame_tests, run_stability_tests, run_pipeline_tests,
               run_performance_tests):
        out = fn(report)
        p, t = out[0], out[1]
        totals[0] += p
        totals[1] += t

    report.append(f"\n---\n\n**TOTAL: {totals[0]}/{totals[1]} tests passed.**")
    text = "\n".join(report)
    with open("validation_report.md", "w") as f:
        f.write(text + "\n")
    print(text)
    if totals[0] != totals[1]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
