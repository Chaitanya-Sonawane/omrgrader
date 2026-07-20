"""
Real-time frame quality validation for OMR auto-capture.

Every camera frame is validated for:
  - sheet detection (outer contour + printed answer grid)
  - distance / coverage
  - blur (Laplacian variance)
  - lighting (under/over exposure, reflections, shadows)
  - perspective distortion (corner angles, border parallelism, aspect ratio)
  - tilt (robust grid-contour box-point edge geometry, NOT minAreaRect angle)
  - grid alignment vs. camera center
  - stability across consecutive frames (per-session tracker)

`check_frame_quality()` returns:
{
    "is_ready": bool,
    "confidence": float,        # 0-100
    "messages": [str, ...],
    "metrics": {
        "blur": ..., "brightness": ..., "tilt": ..., "coverage": ...,
        "perspective": ..., "stability": ..., "grid_alignment": ..., "contrast": ...
    }
}
"""
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

# ---------------- thresholds (tuned for 640-1280 px preview frames) ----------------
BLUR_MIN = 60.0               # Laplacian variance below this -> blurry
BRIGHTNESS_MIN = 70.0
BRIGHTNESS_MAX = 235.0
CONTRAST_MIN = 28.0           # grayscale std-dev
COVERAGE_MIN = 0.18           # sheet grid must occupy >= 18% of frame area
COVERAGE_MAX = 0.92           # too close if grid nearly fills / spills off frame
EDGE_MARGIN_FRAC = 0.015      # grid must stay this far inside frame borders
TILT_MAX_DEG = 4.0            # acceptable rotation of the printed grid
PERSPECTIVE_MAX = 0.10        # max relative difference between opposite edges
ASPECT_TOLERANCE = 0.22       # allowed deviation from expected grid aspect ratio
CENTER_OFFSET_MAX = 0.10      # grid center may deviate <=10% of frame size
REFLECTION_BLOWN_FRAC = 0.015 # fraction of near-255 pixels inside sheet -> glare
SHADOW_RANGE_MAX = 40.0       # max brightness spread across sheet quadrants
STABLE_FRAMES_REQUIRED = 20   # consecutive good+stable frames before capture
CONFIDENCE_REQUIRED = 98.0
EXPECTED_GRID_ASPECT = 1.35   # width/height of the NMMS answer table (2 blocks wide)


def _angle_deg(p, q):
    """Angle of the p->q edge in degrees, normalised to [-45, 45]."""
    ang = math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))
    while ang <= -45:
        ang += 90
    while ang > 45:
        ang -= 90
    return ang


def _order_corners(pts):
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = np.array(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).flatten()
    return np.array([
        pts[np.argmin(s)],   # tl
        pts[np.argmin(d)],   # tr
        pts[np.argmax(s)],   # br
        pts[np.argmax(d)],   # bl
    ], dtype=np.float32)


def _quad_from_contour(c):
    """Extract the 4 true corners of a contour via convex-hull polygon
    approximation; falls back to minAreaRect BOX POINTS (pure geometry -
    never rect[2], whose angle flips unpredictably between -90/0 for
    near-axis-aligned rectangles). True corners are required so keystone
    perspective distortion remains measurable."""
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for eps in (0.02, 0.04, 0.06):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4:
            return _order_corners(approx)
    return _order_corners(cv2.boxPoints(cv2.minAreaRect(c)))


def detect_grid_quad(gray):
    """Locate the printed answer table and return its 4 ordered corners.

    Primary path: morphological horizontal/vertical line extraction (the
    validated grid-contour approach). Fallback for rotated sheets, where
    axis-aligned line kernels break down: merge all ink into a blob and take
    the largest quadrilateral, so tilt guidance can still be given.
    Returns (corners, contour) or (None, None).
    """
    h, w = gray.shape
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 25, 10)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1))
    horiz = cv2.dilate(cv2.erode(th, hk), hk)
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 40)))
    vert = cv2.dilate(cv2.erode(th, vk), vk)
    grid = cv2.add(horiz, vert)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) >= 0.05 * w * h:
            return _quad_from_contour(c), c

    # fallback: rotated table - merge ink into one blob and grab its quad
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    blob = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)
    contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, None
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.05 * w * h:
        return None, None
    return _quad_from_contour(c), c


def grid_tilt_deg(corners):
    """Tilt from box-point edge geometry: mean angle of top & bottom edges."""
    tl, tr, br, bl = corners
    top = _angle_deg(tl, tr)
    bottom = _angle_deg(bl, br)
    return (top + bottom) / 2.0


def perspective_metrics(corners):
    """Relative length mismatch of opposite edges + aspect ratio of the quad."""
    tl, tr, br, bl = corners
    top = np.linalg.norm(tr - tl)
    bottom = np.linalg.norm(br - bl)
    left = np.linalg.norm(bl - tl)
    right = np.linalg.norm(br - tr)
    h_skew = abs(top - bottom) / max(top, bottom, 1e-5)
    v_skew = abs(left - right) / max(left, right, 1e-5)
    aspect = ((top + bottom) / 2.0) / max((left + right) / 2.0, 1e-5)
    return max(h_skew, v_skew), aspect, h_skew, v_skew


def lighting_metrics(gray, corners):
    """Brightness, contrast, glare and shadow analysis inside the sheet quad."""
    mask = np.zeros(gray.shape, dtype=np.uint8)
    cv2.fillPoly(mask, [corners.astype(np.int32)], 255)
    sheet_pixels = gray[mask == 255]
    if sheet_pixels.size == 0:
        sheet_pixels = gray.reshape(-1)

    brightness = float(sheet_pixels.mean())
    contrast = float(sheet_pixels.std())
    blown_frac = float((sheet_pixels >= 250).sum()) / sheet_pixels.size

    # shadow check: compare mean brightness of the 4 quadrants of the quad's bbox
    x, y, w, h = cv2.boundingRect(corners.astype(np.int32))
    x2, y2 = x + w // 2, y + h // 2
    quads = []
    for (qy0, qy1, qx0, qx1) in [(y, y2, x, x2), (y, y2, x2, x + w),
                                 (y2, y + h, x, x2), (y2, y + h, x2, x + w)]:
        qm = mask[qy0:qy1, qx0:qx1]
        qg = gray[qy0:qy1, qx0:qx1]
        vals = qg[qm == 255]
        if vals.size:
            quads.append(float(vals.mean()))
    shadow_range = (max(quads) - min(quads)) if len(quads) >= 2 else 0.0
    return brightness, contrast, blown_frac, shadow_range


# ---------------------------------------------------------------------------
# Stability tracking across consecutive frames (per camera session)
# ---------------------------------------------------------------------------
@dataclass
class FrameSnapshot:
    center: tuple
    area: float
    tilt: float
    blur: float
    ts: float


@dataclass
class StabilityTracker:
    """Counts consecutive frames where position, angle, scale and blur are all
    stable AND the frame itself passed every quality gate. Any movement or a
    bad frame resets the counter."""
    stable_count: int = 0
    last: Optional[FrameSnapshot] = None
    last_seen: float = field(default_factory=time.time)

    # relative motion tolerances between consecutive frames
    POS_TOL = 0.015      # center shift, fraction of frame diagonal
    AREA_TOL = 0.06      # relative area (scale) change
    TILT_TOL = 1.0       # degrees
    BLUR_TOL = 0.45      # relative blur-score change

    def update(self, frame_ok, center, area, tilt, blur, frame_diag):
        now = time.time()
        # a long gap between frames (>1.5s) means the stream was interrupted
        if now - self.last_seen > 1.5:
            self.stable_count = 0
            self.last = None
        self.last_seen = now

        snap = FrameSnapshot(center, area, tilt, blur, now)
        if not frame_ok or center is None:
            self.stable_count = 0
            self.last = snap if center is not None else None
            return 0

        if self.last is not None and self.last.center is not None:
            prev = self.last
            moved = (
                math.hypot(center[0] - prev.center[0], center[1] - prev.center[1])
                > self.POS_TOL * frame_diag
                or abs(area - prev.area) / max(prev.area, 1e-5) > self.AREA_TOL
                or abs(tilt - prev.tilt) > self.TILT_TOL
                or abs(blur - prev.blur) / max(prev.blur, 1e-5) > self.BLUR_TOL
            )
            self.stable_count = 0 if moved else self.stable_count + 1
        else:
            self.stable_count = 0
        self.last = snap
        return self.stable_count


# session_id -> StabilityTracker (in-memory; frames arrive sequentially per session)
_TRACKERS: dict = {}


def get_tracker(session_id: str) -> StabilityTracker:
    # opportunistic cleanup of stale sessions
    now = time.time()
    stale = [k for k, t in _TRACKERS.items() if now - t.last_seen > 300]
    for k in stale:
        _TRACKERS.pop(k, None)
    if session_id not in _TRACKERS:
        _TRACKERS[session_id] = StabilityTracker()
    return _TRACKERS[session_id]


def reset_tracker(session_id: str):
    _TRACKERS.pop(session_id, None)


# ---------------------------------------------------------------------------
# Main frame validation
# ---------------------------------------------------------------------------
def check_frame_quality(image, session_id: str = "default", track_stability: bool = True):
    """Validate one preview frame for OMR auto-capture readiness.

    `image` may be raw encoded bytes (JPEG/PNG) or a BGR numpy array.
    Returns the structured dict documented in the module docstring.
    """
    if isinstance(image, (bytes, bytearray)):
        arr = np.frombuffer(image, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode frame")
    else:
        img = image

    # keep a higher-res grayscale (capped at 1280) for the bubble/template
    # check; run every other check on a fast 640 px copy (<30 ms budget)
    h0, w0 = img.shape[:2]
    scale_hi = min(1.0, 1280.0 / max(h0, w0))
    gray_hi = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if scale_hi < 1:
        gray_hi = cv2.resize(gray_hi, (int(w0 * scale_hi), int(h0 * scale_hi)))
    scale = 640.0 / max(gray_hi.shape)
    gray = cv2.resize(gray_hi, (int(gray_hi.shape[1] * scale),
                                int(gray_hi.shape[0] * scale))) if scale < 1 else gray_hi
    fh, fw = gray.shape
    frame_area = float(fh * fw)
    frame_diag = math.hypot(fw, fh)

    messages = []
    penalties = 0.0          # confidence = 100 - sum(penalties)
    hard_fail = False        # any rejection rule tripped

    def reject(msg, penalty=25.0):
        nonlocal penalties, hard_fail
        if msg not in messages:
            messages.append(msg)
        penalties += penalty
        hard_fail = True

    def warn(msg, penalty=4.0):
        nonlocal penalties
        if msg not in messages:
            messages.append(msg)
        penalties += penalty

    metrics = {
        "blur": None, "brightness": None, "tilt": None, "coverage": None,
        "perspective": None, "stability": 0, "grid_alignment": None,
        "contrast": None,
    }

    # ---- blur (whole frame; cheap, catches motion blur even w/o a sheet) ----
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    metrics["blur"] = round(blur, 1)
    if blur < BLUR_MIN:
        reject("Image Blurry")
        messages.append("Hold Steady")

    # ---- sheet / grid detection ----
    corners, contour = detect_grid_quad(gray)
    if corners is None:
        reject("Entire Sheet Not Visible", 60.0)
        messages.append("Align Sheet")
        metrics["brightness"] = round(float(gray.mean()), 1)
        metrics["contrast"] = round(float(gray.std()), 1)
        if track_stability:
            get_tracker(session_id).update(False, None, 0, 0, blur, frame_diag)
        return _result(False, penalties, messages, metrics)

    quad_area = float(cv2.contourArea(corners))
    coverage = quad_area / frame_area
    metrics["coverage"] = round(coverage, 3)

    # ---- distance / cropping ----
    mx, my = EDGE_MARGIN_FRAC * fw, EDGE_MARGIN_FRAC * fh
    xs, ys = corners[:, 0], corners[:, 1]
    if xs.min() < mx or ys.min() < my or xs.max() > fw - mx or ys.max() > fh - my:
        reject("Sheet Cropped")
        messages.append("Move Away")
    if coverage < COVERAGE_MIN:
        reject("Move Closer")
    elif coverage > COVERAGE_MAX:
        reject("Move Away")

    # ---- tilt (validated grid-contour box-point approach) ----
    tilt = grid_tilt_deg(corners)
    metrics["tilt"] = round(tilt, 2)
    if abs(tilt) > TILT_MAX_DEG:
        reject("Rotate Left" if tilt > 0 else "Rotate Right")

    # ---- perspective ----
    skew, aspect, h_skew, v_skew = perspective_metrics(corners)
    metrics["perspective"] = round(skew, 3)
    if skew > PERSPECTIVE_MAX:
        reject("Align Sheet")
    if abs(aspect - EXPECTED_GRID_ASPECT) / EXPECTED_GRID_ASPECT > ASPECT_TOLERANCE:
        reject("Align Sheet")

    # ---- grid alignment vs. camera center ----
    gcx, gcy = float(xs.mean()), float(ys.mean())
    dx, dy = (gcx - fw / 2) / fw, (gcy - fh / 2) / fh
    metrics["grid_alignment"] = {"dx": round(dx, 3), "dy": round(dy, 3)}
    if abs(dx) > CENTER_OFFSET_MAX:
        reject("Move Right" if dx < 0 else "Move Left", 15.0)
    if abs(dy) > CENTER_OFFSET_MAX:
        reject("Move Down" if dy < 0 else "Move Up", 15.0)

    # ---- lighting ----
    brightness, contrast, blown_frac, shadow_range = lighting_metrics(gray, corners)
    metrics["brightness"] = round(brightness, 1)
    metrics["contrast"] = round(contrast, 1)
    if brightness < BRIGHTNESS_MIN:
        reject("Too Dark")
    elif brightness > BRIGHTNESS_MAX:
        reject("Too Bright")
    if blown_frac > REFLECTION_BLOWN_FRAC:
        reject("Reflection Detected")
    if shadow_range > SHADOW_RANGE_MAX:
        reject("Shadow Detected")
    if contrast < CONTRAST_MIN:
        warn("Too Dark" if brightness < 128 else "Too Bright", 10.0)

    # ---- template recognition: grid must contain enough bubble circles ----
    # (counted on the higher-res copy - small bubbles vanish at 640 px)
    up = 1.0 / scale if scale < 1 else 1.0
    x, y, w, h = cv2.boundingRect((corners * up).astype(np.int32))
    roi = gray_hi[max(0, y):y + h, max(0, x):x + w]
    n_bubbles = _count_bubbles(roi) if roi.size else 0
    if n_bubbles < 80:  # expect 160; >=50% needed to trust the template
        reject("Align Sheet", 30.0)
        messages.append("Entire Sheet Not Visible")

    frame_ok = not hard_fail

    # ---- stability across consecutive frames ----
    stable = 0
    if track_stability:
        stable = get_tracker(session_id).update(
            frame_ok, (gcx, gcy), quad_area, tilt, blur, frame_diag)
    metrics["stability"] = stable

    if frame_ok and stable < STABLE_FRAMES_REQUIRED:
        messages.append("Hold Steady")
        # small, shrinking penalty so confidence rises as stability builds
        penalties += max(0.0, 1.9 * (1 - stable / STABLE_FRAMES_REQUIRED))

    confidence = max(0.0, 100.0 - penalties)
    is_ready = (frame_ok and stable >= STABLE_FRAMES_REQUIRED
                and confidence >= CONFIDENCE_REQUIRED)
    if is_ready:
        messages = ["Ready to Capture"]
    return _result(is_ready, penalties, messages, metrics)


def _to_native(v):
    """Recursively convert numpy scalars to JSON-serialisable Python types."""
    if isinstance(v, dict):
        return {k: _to_native(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_native(x) for x in v]
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v


def _result(is_ready, penalties, messages, metrics):
    return {
        "is_ready": bool(is_ready),
        "confidence": round(max(0.0, 100.0 - float(penalties)), 1),
        "messages": messages,
        "metrics": _to_native(metrics),
    }


def _count_bubbles(roi_gray):
    """Quick circle count inside the grid ROI (template recognition check)."""
    if roi_gray.shape[0] < 40 or roi_gray.shape[1] < 40:
        return 0
    blur = cv2.GaussianBlur(roi_gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 21, 8)
    cnts, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    h, w = roi_gray.shape
    approx_cell = min(w / 10, h / 21)
    r_lo, r_hi = approx_cell * 0.18, approx_cell * 0.42
    n = 0
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 20:
            continue
        (_, _), radius = cv2.minEnclosingCircle(c)
        if r_lo <= radius <= r_hi:
            circ = area / (np.pi * radius * radius + 1e-5)
            if circ >= 0.45:
                n += 1
    return n


# ---------------------------------------------------------------------------
# Post-capture processing: perspective correction, deskew, contrast
# enhancement, shadow removal - produces a clean image for bubble detection.
# ---------------------------------------------------------------------------
def process_captured_image(image_bytes: bytes) -> bytes:
    """Clean up an auto-captured frame and return re-encoded JPEG bytes.

    Steps: perspective correction + deskew (warp the sheet quad flat with a
    margin around the grid), shadow removal (background division), contrast
    enhancement (CLAHE). Adaptive thresholding for bubble detection happens
    downstream inside the scan engine, which expects grayscale-like input.
    """
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode captured image")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    corners, _ = detect_grid_quad(gray)

    if corners is not None:
        tl, tr, br, bl = corners
        gw = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
        gh = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
        # keep a margin around the grid so the engine sees the full table
        margin = int(0.04 * max(gw, gh))
        dst = np.array([
            [margin, margin], [margin + gw, margin],
            [margin + gw, margin + gh], [margin, margin + gh],
        ], dtype=np.float32)
        M = cv2.getPerspectiveTransform(corners, dst)
        img = cv2.warpPerspective(img, M, (gw + 2 * margin, gh + 2 * margin),
                                  flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)

    # shadow removal: divide by a heavily-blurred background estimate
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bg = cv2.medianBlur(cv2.dilate(g, np.ones((7, 7), np.uint8)), 31)
    norm = cv2.divide(g, bg, scale=255)

    # contrast enhancement
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(norm)

    out = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise ValueError("Could not encode processed image")
    return buf.tobytes()
