"""
Core OMR detection engine calibrated for the NMMS-style answer sheet
(2 blocks of 20 questions x 4 options, circular bubbles, table-ruled).

Pipeline:
1. Load + auto-orient (EXIF) + resize
2. Locate the ruled answer-grid (largest line-grid contour)
3. Detect all bubble circles inside the grid (contour + circularity filter)
4. Cluster circle centers into a canonical 20-row x 8-column bubble grid
   (robust to missing/dashed lines - anchors on the bubbles themselves)
5. For each of the 160 bubble cells, compute a multi-metric fill score
6. Resolve each question -> answer option (1-4), flag blank/multiple
"""
import cv2
import numpy as np
from sklearn.cluster import KMeans
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BubbleResult:
    question: int
    selected: Optional[int]   # 1-4, None if blank
    is_multiple: bool
    multiple_options: list
    confidences: dict         # option -> fill confidence %
    flagged: bool = False
    flag_reason: str = ""


@dataclass
class ScanResult:
    answers: dict              # question -> BubbleResult
    quality: dict               # blur, brightness, etc.
    debug_image: Optional[np.ndarray] = None
    warnings: list = field(default_factory=list)


def load_image_exif_safe(path_or_bytes):
    """Load image respecting EXIF orientation."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        arr = np.frombuffer(path_or_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    else:
        img = cv2.imread(path_or_bytes, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    return img


def blur_score(gray):
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def resize_max(img, max_dim=1600):
    h, w = img.shape[:2]
    scale = max_dim / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    return img


def find_answer_grid_bbox(gray):
    """Find the bounding box of the ruled answer table using morphological
    horizontal/vertical line extraction."""
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
    if not contours:
        return None
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    c = contours[0]
    x, y, ww, hh = cv2.boundingRect(c)
    # sanity: grid should be a big chunk of the image
    if ww * hh < 0.15 * w * h:
        return None
    return x, y, ww, hh


def detect_circles(roi_gray):
    """Detect circular bubble candidates within the answer-grid ROI."""
    blur = cv2.GaussianBlur(roi_gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 21, 8)
    cnts, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    h, w = roi_gray.shape
    # expected bubble radius: table is divided into ~10 cols x 21 rows
    approx_cell = min(w / 10, h / 21)
    r_lo, r_hi = approx_cell * 0.18, approx_cell * 0.42

    raw = []
    for c in cnts:
        area = cv2.contourArea(c)
        if area < 30:
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(c)
        if radius < r_lo or radius > r_hi:
            continue
        circularity = area / (np.pi * radius * radius + 1e-5)
        if circularity < 0.45:
            continue
        raw.append((cx, cy, radius))

    # non-max suppression: merge near-duplicate detections (outer/inner contours)
    raw.sort(key=lambda t: -t[2])
    kept = []
    for cx, cy, r in raw:
        dup = False
        for kx, ky, kr in kept:
            if (cx - kx) ** 2 + (cy - ky) ** 2 < (max(r, kr) * 0.9) ** 2:
                dup = True
                break
        if not dup:
            kept.append((cx, cy, r))
    return kept


def cluster_grid(circles, n_rows=20, n_cols=8):
    """Cluster raw circle detections into a canonical row x col grid.
    Returns dict[(row,col)] -> (cx,cy,r) and calibration diagnostics."""
    if len(circles) < n_rows * n_cols * 0.5:
        raise ValueError(
            f"Only {len(circles)} bubble candidates found; expected ~{n_rows*n_cols}. "
            "Image quality too low for reliable detection."
        )

    pts = np.array([[c[0], c[1]] for c in circles])

    # cluster columns first (8 option-columns across 2 blocks)
    col_km = KMeans(n_clusters=n_cols, n_init=10, random_state=0).fit(pts[:, 0:1])
    col_centers = col_km.cluster_centers_.flatten()
    col_order = np.argsort(col_centers)
    col_rank = {old: new for new, old in enumerate(col_order)}
    col_labels = np.array([col_rank[l] for l in col_km.labels_])

    # cluster rows (20 question rows) using y only
    row_km = KMeans(n_clusters=n_rows, n_init=10, random_state=0).fit(pts[:, 1:2])
    row_centers = row_km.cluster_centers_.flatten()
    row_order = np.argsort(row_centers)
    row_rank = {old: new for new, old in enumerate(row_order)}
    row_labels = np.array([row_rank[l] for l in row_km.labels_])

    grid = {}
    for i, (cx, cy, r) in enumerate(circles):
        key = (row_labels[i], col_labels[i])
        # if collision keep the more "circular"/plausible one (first wins is fine
        # since duplicates already suppressed)
        if key not in grid:
            grid[key] = (cx, cy, r)

    return grid, sorted(row_centers[row_order]), sorted(col_centers[col_order])


def fill_metrics(gray_roi, cx, cy, r):
    """Compute fill confidence for a single bubble using multiple metrics."""
    h, w = gray_roi.shape
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r))
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r))
    patch = gray_roi[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0, 0.0

    mask = np.zeros(patch.shape, dtype=np.uint8)
    pcx, pcy = cx - x0, cy - y0
    inner_r = max(1, int(r * 0.72))  # avoid counting the printed ring itself
    cv2.circle(mask, (int(pcx), int(pcy)), inner_r, 255, -1)

    inner_pixels = patch[mask == 255]
    if inner_pixels.size == 0:
        return 0.0, 0.0

    mean_intensity = float(inner_pixels.mean())
    # local threshold: compare against the bubble's own local background sample
    ring_mask = np.zeros(patch.shape, dtype=np.uint8)
    cv2.circle(ring_mask, (int(pcx), int(pcy)), max(1, int(r * 0.95)), 255, -1)
    cv2.circle(ring_mask, (int(pcx), int(pcy)), inner_r, 0, -1)
    dark_ratio = float((inner_pixels < 150).sum()) / inner_pixels.size

    return mean_intensity, dark_ratio


def score_all_bubbles(gray_roi, grid, n_rows, n_cols):
    """Returns dict[(row,col)] -> confidence(0-100, higher = more filled)."""
    raw_scores = {}
    for (row, col), (cx, cy, r) in grid.items():
        mean_i, dark_ratio = fill_metrics(gray_roi, cx, cy, r)
        raw_scores[(row, col)] = (mean_i, dark_ratio)

    # normalize mean intensity per-row (handles lighting gradient across sheet)
    confidences = {}
    for row in range(n_rows):
        row_vals = [raw_scores[(row, c)][0] for c in range(n_cols) if (row, c) in raw_scores]
        if not row_vals:
            continue
        lo, hi = min(row_vals), max(row_vals)
        span = max(hi - lo, 1e-5)
        for col in range(n_cols):
            if (row, col) not in raw_scores:
                continue
            mean_i, dark_ratio = raw_scores[(row, col)]
            # darker (lower mean intensity) relative to row peers = more filled
            rel_fill = (hi - mean_i) / span
            score = 0.55 * rel_fill + 0.45 * dark_ratio
            confidences[(row, col)] = round(float(score) * 100, 1)
    return confidences


FILL_THRESHOLD = 42.0       # confidence % above which a bubble counts as marked
AMBIGUOUS_MARGIN = 12.0     # if 2nd place is within this margin of 1st -> multi-mark flag


def resolve_answers(confidences, n_rows=20, n_cols=8, block_size=4):
    """Convert per-bubble confidences into per-question answers.
    Columns 0-3 = block A (Q1-20), columns 4-7 = block B (Q21-40)."""
    results = {}
    for row in range(n_rows):
        for block, col_offset, q_offset in [(0, 0, 0), (1, 4, 20)]:
            qnum = row + 1 + q_offset
            opt_scores = {}
            for opt in range(4):
                col = col_offset + opt
                conf = confidences.get((row, col))
                if conf is not None:
                    opt_scores[opt + 1] = conf
            if not opt_scores:
                results[qnum] = BubbleResult(qnum, None, False, [], {}, True, "not detected")
                continue
            marked = [o for o, c in opt_scores.items() if c >= FILL_THRESHOLD]
            marked.sort(key=lambda o: -opt_scores[o])

            if len(marked) == 0:
                results[qnum] = BubbleResult(qnum, None, False, [], opt_scores, False, "blank")
            elif len(marked) == 1:
                results[qnum] = BubbleResult(qnum, marked[0], False, [], opt_scores)
            else:
                top, second = marked[0], marked[1]
                if opt_scores[top] - opt_scores[second] < AMBIGUOUS_MARGIN:
                    results[qnum] = BubbleResult(
                        qnum, None, True, marked, opt_scores, True, "multiple marks"
                    )
                else:
                    results[qnum] = BubbleResult(qnum, top, False, [], opt_scores)
    return results


def scan_omr_sheet(image_bytes_or_path, n_rows=20, n_cols=8, debug=False) -> ScanResult:
    img = load_image_exif_safe(image_bytes_or_path)
    img = resize_max(img, 1600)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    warnings = []
    bscore = blur_score(gray)
    brightness = float(gray.mean())
    if bscore < 60:
        warnings.append(f"Image may be blurry (sharpness score {bscore:.0f}, recommend >60)")
    if brightness < 60:
        warnings.append("Image is quite dark; consider better lighting")
    if brightness > 220:
        warnings.append("Image is overexposed; consider reducing glare")

    bbox = find_answer_grid_bbox(gray)
    if bbox is None:
        raise ValueError("Could not locate the answer grid in this image. "
                          "Ensure the full sheet with its ruled table is visible.")
    x, y, w, h = bbox
    roi_gray = gray[y:y + h, x:x + w]

    circles = detect_circles(roi_gray)
    grid, row_centers, col_centers = cluster_grid(circles, n_rows, n_cols)

    confidences = score_all_bubbles(roi_gray, grid, n_rows, n_cols)
    answers = resolve_answers(confidences, n_rows, n_cols)

    debug_img = None
    if debug:
        debug_img = img.copy()
        for (row, col), (cx, cy, r) in grid.items():
            conf = confidences.get((row, col), 0)
            color = (0, 0, 255) if conf >= FILL_THRESHOLD else (0, 200, 0)
            cv2.circle(debug_img, (int(cx + x), int(cy + y)), int(r), color, 2)

    return ScanResult(answers=answers, quality={
        "blur_score": round(bscore, 1),
        "brightness": round(brightness, 1),
        "bubbles_detected": len(circles),
    }, debug_image=debug_img, warnings=warnings)
