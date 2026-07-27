package com.omr.capture

import org.opencv.core.Core
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.MatOfPoint
import org.opencv.core.MatOfPoint2f
import org.opencv.core.Point
import org.opencv.core.Rect
import org.opencv.core.Scalar
import org.opencv.core.Size
import org.opencv.imgproc.CLAHE
import org.opencv.imgproc.Imgproc
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * On-device port of `omr_engine.py` (+ the post-capture cleanup from
 * `frame_quality.process_captured_image`). Detects bubble circles inside the
 * ruled answer grid, clusters them into a 20x8 grid with a lightweight 1-D
 * k-means, scores each bubble's fill and resolves per-question answers.
 *
 * Calibrated for the NMMS sheet: 40 questions in 2 blocks of 20, 4 options each.
 */
object OmrEngine {

    private const val N_ROWS = 20
    private const val N_COLS = 8
    private const val FILL_THRESHOLD = 42.0
    private const val AMBIGUOUS_MARGIN = 12.0

    /** Per-question detection result. selected is 1-4 or null when blank. */
    data class BubbleResult(
        val question: Int,
        val selected: Int?,
        val isMultiple: Boolean,
        val flagged: Boolean = false,
        val flagReason: String = "",
    )

    data class ScanResult(
        val answers: Map<Int, BubbleResult>,
        val bubblesDetected: Int,
        val blurScore: Double,
        val brightness: Double,
        val warnings: List<String>,
    )

    // ---------------------------------------------------------------------
    // Post-capture cleanup: perspective correction + deskew, shadow removal
    // (background division), contrast enhancement (CLAHE).
    // ---------------------------------------------------------------------
    fun process(bgr: Mat): Mat {
        val gray = Mat()
        Imgproc.cvtColor(bgr, gray, Imgproc.COLOR_BGR2GRAY)
        val corners = FrameQuality.detectGridQuad(gray)

        var work = bgr
        if (corners != null) {
            val (tl, tr, br, bl) = corners
            val gw = max(hyp(tr, tl), hyp(br, bl)).roundToInt()
            val gh = max(hyp(bl, tl), hyp(br, tr)).roundToInt()
            val margin = (0.04 * max(gw, gh)).roundToInt()
            val src = MatOfPoint2f(*corners)
            val dst = MatOfPoint2f(
                Point(margin.toDouble(), margin.toDouble()),
                Point((margin + gw).toDouble(), margin.toDouble()),
                Point((margin + gw).toDouble(), (margin + gh).toDouble()),
                Point(margin.toDouble(), (margin + gh).toDouble()),
            )
            val m = Imgproc.getPerspectiveTransform(src, dst)
            val warped = Mat()
            Imgproc.warpPerspective(
                bgr, warped, m, Size((gw + 2 * margin).toDouble(), (gh + 2 * margin).toDouble()),
                Imgproc.INTER_CUBIC, Core.BORDER_REPLICATE, Scalar(0.0)
            )
            work = warped
        }

        val g = Mat()
        Imgproc.cvtColor(work, g, Imgproc.COLOR_BGR2GRAY)
        val dil = Mat()
        Imgproc.dilate(g, dil, Mat.ones(Size(7.0, 7.0), CvType.CV_8U))
        val bg = Mat()
        Imgproc.medianBlur(dil, bg, 31)
        val norm = Mat()
        Core.divide(g, bg, norm, 255.0)

        val clahe: CLAHE = Imgproc.createCLAHE(2.0, Size(8.0, 8.0))
        val enhanced = Mat()
        clahe.apply(norm, enhanced)

        val out = Mat()
        Imgproc.cvtColor(enhanced, out, Imgproc.COLOR_GRAY2BGR)
        return out
    }

    private fun hyp(a: Point, b: Point) = Math.hypot(a.x - b.x, a.y - b.y)

    // ---------------------------------------------------------------------
    // Scan: locate grid bbox, detect circles, cluster, score, resolve.
    // ---------------------------------------------------------------------
    fun scan(bgr: Mat): ScanResult {
        val img = resizeMax(bgr, 1600)
        val gray = Mat()
        Imgproc.cvtColor(img, gray, Imgproc.COLOR_BGR2GRAY)

        val warnings = ArrayList<String>()
        val bscore = laplacianVar(gray)
        val brightness = Core.mean(gray).`val`[0]
        if (bscore < 60) warnings.add("Image may be blurry (sharpness ${bscore.roundToInt()}, recommend >60)")
        if (brightness < 60) warnings.add("Image is quite dark; consider better lighting")
        if (brightness > 220) warnings.add("Image is overexposed; consider reducing glare")

        val bbox = findAnswerGridBbox(gray)
            ?: throw IllegalStateException("Could not locate the answer grid. Ensure the full ruled table is visible.")
        val roi = gray.submat(bbox)

        val circles = detectCircles(roi)
        if (circles.size < N_ROWS * N_COLS * 0.5) {
            throw IllegalStateException("Only ${circles.size} bubbles found; expected ~${N_ROWS * N_COLS}. Image quality too low.")
        }

        val grid = clusterGrid(circles)
        val confidences = scoreAllBubbles(roi, grid)
        val answers = resolveAnswers(confidences)

        return ScanResult(answers, circles.size, round1(bscore), round1(brightness), warnings)
    }

    private data class Circle(val cx: Double, val cy: Double, val r: Double)

    private fun findAnswerGridBbox(gray: Mat): Rect? {
        val w = gray.cols(); val h = gray.rows()
        val blur = Mat(); Imgproc.GaussianBlur(gray, blur, Size(5.0, 5.0), 0.0)
        val th = Mat()
        Imgproc.adaptiveThreshold(blur, th, 255.0, Imgproc.ADAPTIVE_THRESH_GAUSSIAN_C, Imgproc.THRESH_BINARY_INV, 25, 10.0)
        val hk = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, Size(max(20, w / 30).toDouble(), 1.0))
        val horiz = Mat(); Imgproc.erode(th, horiz, hk); Imgproc.dilate(horiz, horiz, hk)
        val vk = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, Size(1.0, max(20, h / 40).toDouble()))
        val vert = Mat(); Imgproc.erode(th, vert, vk); Imgproc.dilate(vert, vert, vk)
        val grid = Mat(); Core.add(horiz, vert, grid)
        val contours = ArrayList<MatOfPoint>()
        Imgproc.findContours(grid, contours, Mat(), Imgproc.RETR_EXTERNAL, Imgproc.CHAIN_APPROX_SIMPLE)
        if (contours.isEmpty()) return null
        val c = contours.maxByOrNull { Imgproc.contourArea(it) }!!
        val r = Imgproc.boundingRect(c)
        if (r.width.toDouble() * r.height < 0.15 * w * h) return null
        return r
    }

    private fun detectCircles(roi: Mat): List<Circle> {
        val blur = Mat(); Imgproc.GaussianBlur(roi, blur, Size(5.0, 5.0), 0.0)
        val th = Mat()
        Imgproc.adaptiveThreshold(blur, th, 255.0, Imgproc.ADAPTIVE_THRESH_GAUSSIAN_C, Imgproc.THRESH_BINARY_INV, 21, 8.0)
        val cnts = ArrayList<MatOfPoint>()
        Imgproc.findContours(th, cnts, Mat(), Imgproc.RETR_LIST, Imgproc.CHAIN_APPROX_SIMPLE)
        val approxCell = min(roi.cols() / 10.0, roi.rows() / 21.0)
        val rLo = approxCell * 0.18; val rHi = approxCell * 0.42

        val raw = ArrayList<Circle>()
        for (c in cnts) {
            val area = Imgproc.contourArea(c)
            if (area < 30) continue
            val center = Point(); val radius = FloatArray(1)
            Imgproc.minEnclosingCircle(MatOfPoint2f(*c.toArray()), center, radius)
            val rad = radius[0].toDouble()
            if (rad < rLo || rad > rHi) continue
            val circ = area / (Math.PI * rad * rad + 1e-5)
            if (circ < 0.45) continue
            raw.add(Circle(center.x, center.y, rad))
        }

        // non-max suppression: merge near-duplicate detections
        raw.sortByDescending { it.r }
        val kept = ArrayList<Circle>()
        for (c in raw) {
            var dup = false
            for (k in kept) {
                val d2 = (c.cx - k.cx) * (c.cx - k.cx) + (c.cy - k.cy) * (c.cy - k.cy)
                if (d2 < (max(c.r, k.r) * 0.9) * (max(c.r, k.r) * 0.9)) { dup = true; break }
            }
            if (!dup) kept.add(c)
        }
        return kept
    }

    private fun clusterGrid(circles: List<Circle>): Map<Pair<Int, Int>, Circle> {
        val colLabels = kmeans1d(circles.map { it.cx }, N_COLS)
        val rowLabels = kmeans1d(circles.map { it.cy }, N_ROWS)
        val grid = HashMap<Pair<Int, Int>, Circle>()
        for (i in circles.indices) {
            val key = Pair(rowLabels[i], colLabels[i])
            if (key !in grid) grid[key] = circles[i]
        }
        return grid
    }

    /** Lloyd's 1-D k-means; returns rank-ordered cluster labels (0..k-1 by value). */
    private fun kmeans1d(values: List<Double>, k: Int, iters: Int = 25): IntArray {
        val n = values.size
        val sorted = values.sorted()
        // init centers evenly across the sorted range
        val centers = DoubleArray(k) { sorted[((it + 0.5) / k * (n - 1)).toInt().coerceIn(0, n - 1)] }
        val labels = IntArray(n)
        repeat(iters) {
            for (i in 0 until n) {
                var best = 0; var bestD = Double.MAX_VALUE
                for (c in 0 until k) {
                    val d = Math.abs(values[i] - centers[c])
                    if (d < bestD) { bestD = d; best = c }
                }
                labels[i] = best
            }
            val sum = DoubleArray(k); val cnt = IntArray(k)
            for (i in 0 until n) { sum[labels[i]] += values[i]; cnt[labels[i]]++ }
            for (c in 0 until k) if (cnt[c] > 0) centers[c] = sum[c] / cnt[c]
        }
        // rank centers so label 0 = smallest coordinate, matching the Python order
        val order = (0 until k).sortedBy { centers[it] }
        val rank = IntArray(k)
        for (newIdx in order.indices) rank[order[newIdx]] = newIdx
        return IntArray(n) { rank[labels[it]] }
    }

    /** dark-fill confidence 0-100 per bubble, normalized per-row for lighting. */
    private fun scoreAllBubbles(roi: Mat, grid: Map<Pair<Int, Int>, Circle>): Map<Pair<Int, Int>, Double> {
        val raw = HashMap<Pair<Int, Int>, Pair<Double, Double>>()
        for ((key, c) in grid) raw[key] = fillMetrics(roi, c)

        val confidences = HashMap<Pair<Int, Int>, Double>()
        for (row in 0 until N_ROWS) {
            val rowVals = (0 until N_COLS).mapNotNull { raw[Pair(row, it)]?.first }
            if (rowVals.isEmpty()) continue
            val lo = rowVals.min(); val hi = rowVals.max()
            val span = max(hi - lo, 1e-5)
            for (col in 0 until N_COLS) {
                val m = raw[Pair(row, col)] ?: continue
                val relFill = (hi - m.first) / span
                val score = 0.55 * relFill + 0.45 * m.second
                confidences[Pair(row, col)] = round1(score * 100)
            }
        }
        return confidences
    }

    /** returns (meanIntensity, darkRatio) inside the bubble's inner disc. */
    private fun fillMetrics(roi: Mat, c: Circle): Pair<Double, Double> {
        val w = roi.cols(); val h = roi.rows()
        val x0 = max(0, (c.cx - c.r).toInt()); val x1 = min(w, (c.cx + c.r).toInt())
        val y0 = max(0, (c.cy - c.r).toInt()); val y1 = min(h, (c.cy + c.r).toInt())
        if (x1 <= x0 || y1 <= y0) return Pair(255.0, 0.0)
        val patch = roi.submat(Rect(x0, y0, x1 - x0, y1 - y0))

        val mask = Mat.zeros(patch.size(), CvType.CV_8UC1)
        val pcx = c.cx - x0; val pcy = c.cy - y0
        val innerR = max(1, (c.r * 0.72).toInt())
        Imgproc.circle(mask, Point(pcx, pcy), innerR, Scalar(255.0), -1)

        val meanIntensity = Core.mean(patch, mask).`val`[0]

        val dark = Mat()
        Imgproc.threshold(patch, dark, 149.0, 255.0, Imgproc.THRESH_BINARY_INV)
        Core.bitwise_and(dark, mask, dark)
        val innerCount = Core.countNonZero(mask).coerceAtLeast(1)
        val darkRatio = Core.countNonZero(dark).toDouble() / innerCount
        return Pair(meanIntensity, darkRatio)
    }

    private fun resolveAnswers(conf: Map<Pair<Int, Int>, Double>): Map<Int, BubbleResult> {
        val results = HashMap<Int, BubbleResult>()
        for (row in 0 until N_ROWS) {
            for ((colOffset, qOffset) in listOf(Pair(0, 0), Pair(4, 20))) {
                val qnum = row + 1 + qOffset
                val optScores = HashMap<Int, Double>()
                for (opt in 0 until 4) {
                    conf[Pair(row, colOffset + opt)]?.let { optScores[opt + 1] = it }
                }
                if (optScores.isEmpty()) {
                    results[qnum] = BubbleResult(qnum, null, false, true, "not detected"); continue
                }
                val marked = optScores.filter { it.value >= FILL_THRESHOLD }.keys
                    .sortedByDescending { optScores[it] }
                results[qnum] = when {
                    marked.isEmpty() -> BubbleResult(qnum, null, false, false, "blank")
                    marked.size == 1 -> BubbleResult(qnum, marked[0], false)
                    else -> {
                        val top = marked[0]; val second = marked[1]
                        if (optScores[top]!! - optScores[second]!! < AMBIGUOUS_MARGIN)
                            BubbleResult(qnum, null, true, true, "multiple marks")
                        else BubbleResult(qnum, top, false)
                    }
                }
            }
        }
        return results
    }

    private fun resizeMax(img: Mat, maxDim: Int): Mat {
        val scale = maxDim.toDouble() / max(img.rows(), img.cols())
        if (scale >= 1) return img
        val out = Mat()
        Imgproc.resize(img, out, Size(img.cols() * scale, img.rows() * scale))
        return out
    }

    private fun laplacianVar(gray: Mat): Double {
        val lap = Mat(); Imgproc.Laplacian(gray, lap, CvType.CV_64F)
        val std = MatOfDoubleWrap()
        Core.meanStdDev(lap, MatOfDoubleWrap().mat, std.mat)
        val s = std.value(); return s * s
    }

    private fun round1(v: Double) = Math.round(v * 10.0) / 10.0
}
