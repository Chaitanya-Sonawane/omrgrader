package com.omr.capture

import org.opencv.core.Core
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.MatOfInt
import org.opencv.core.MatOfPoint
import org.opencv.core.MatOfPoint2f
import org.opencv.core.Point
import org.opencv.core.Rect
import org.opencv.core.Scalar
import org.opencv.core.Size
import org.opencv.imgproc.Imgproc
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * On-device port of the backend `frame_quality.py`.
 *
 * Every camera preview frame is validated for sheet detection, distance,
 * blur, lighting, perspective, tilt, centering, template recognition and
 * cross-frame stability. Operates directly on a grayscale [Mat] (the camera
 * Y-plane), so no JPEG decode is needed on the hot path.
 */
object FrameQuality {

    // ---- thresholds (tuned for 640-1280 px preview frames) ----
    private const val BLUR_MIN = 60.0
    private const val BRIGHTNESS_MIN = 70.0
    private const val BRIGHTNESS_MAX = 235.0
    private const val CONTRAST_MIN = 28.0
    private const val COVERAGE_MIN = 0.18
    private const val COVERAGE_MAX = 0.92
    private const val EDGE_MARGIN_FRAC = 0.015
    private const val TILT_MAX_DEG = 4.0
    private const val PERSPECTIVE_MAX = 0.10
    private const val ASPECT_TOLERANCE = 0.22
    private const val CENTER_OFFSET_MAX = 0.10
    private const val REFLECTION_BLOWN_FRAC = 0.015
    private const val SHADOW_RANGE_MAX = 40.0
    const val STABLE_FRAMES_REQUIRED = 20
    private const val CONFIDENCE_REQUIRED = 98.0
    private const val EXPECTED_GRID_ASPECT = 1.35

    data class Result(
        val isReady: Boolean,
        val confidence: Double,
        val messages: List<String>,
        val stability: Int,
        val tilt: Double,
        val dx: Double,
        val dy: Double,
    )

    private fun angleDeg(p: Point, q: Point): Double {
        var ang = Math.toDegrees(atan2(q.y - p.y, q.x - p.x))
        while (ang <= -45) ang += 90
        while (ang > 45) ang -= 90
        return ang
    }

    /** Order 4 points as top-left, top-right, bottom-right, bottom-left. */
    private fun orderCorners(pts: Array<Point>): Array<Point> {
        val tl = pts.minByOrNull { it.x + it.y }!!
        val br = pts.maxByOrNull { it.x + it.y }!!
        val tr = pts.minByOrNull { it.y - it.x }!!
        val bl = pts.maxByOrNull { it.y - it.x }!!
        return arrayOf(tl, tr, br, bl)
    }

    private fun quadFromContour(c: MatOfPoint): Array<Point> {
        val hullIdx = MatOfInt()
        Imgproc.convexHull(c, hullIdx)
        val cPts = c.toArray()
        val hullPts = hullIdx.toArray().map { cPts[it] }.toTypedArray()
        val hull2f = MatOfPoint2f(*hullPts)
        val peri = Imgproc.arcLength(hull2f, true)
        for (eps in doubleArrayOf(0.02, 0.04, 0.06)) {
            val approx = MatOfPoint2f()
            Imgproc.approxPolyDP(hull2f, approx, eps * peri, true)
            if (approx.total() == 4L) {
                return orderCorners(approx.toArray())
            }
        }
        // fallback: min-area-rect box points (pure geometry, never the angle)
        val rr = Imgproc.minAreaRect(MatOfPoint2f(*cPts))
        val box = arrayOfNulls<Point>(4)
        rr.points(box)
        @Suppress("UNCHECKED_CAST")
        return orderCorners(box.map { it!! }.toTypedArray())
    }

    /** Locate the printed answer table; returns its 4 ordered corners or null. */
    fun detectGridQuad(gray: Mat): Array<Point>? {
        val w = gray.cols()
        val h = gray.rows()
        val blur = Mat()
        Imgproc.GaussianBlur(gray, blur, Size(5.0, 5.0), 0.0)
        val th = Mat()
        Imgproc.adaptiveThreshold(
            blur, th, 255.0, Imgproc.ADAPTIVE_THRESH_GAUSSIAN_C,
            Imgproc.THRESH_BINARY_INV, 25, 10.0
        )
        val hk = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, Size(max(20, w / 30).toDouble(), 1.0))
        val horiz = Mat()
        Imgproc.erode(th, horiz, hk); Imgproc.dilate(horiz, horiz, hk)
        val vk = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, Size(1.0, max(20, h / 40).toDouble()))
        val vert = Mat()
        Imgproc.erode(th, vert, vk); Imgproc.dilate(vert, vert, vk)
        val grid = Mat()
        Core.add(horiz, vert, grid)

        fun largestQuad(src: Mat): Array<Point>? {
            val contours = ArrayList<MatOfPoint>()
            Imgproc.findContours(src, contours, Mat(), Imgproc.RETR_EXTERNAL, Imgproc.CHAIN_APPROX_SIMPLE)
            if (contours.isEmpty()) return null
            val c = contours.maxByOrNull { Imgproc.contourArea(it) }!!
            if (Imgproc.contourArea(c) < 0.05 * w * h) return null
            return quadFromContour(c)
        }

        largestQuad(grid.clone())?.let { return it }

        // fallback: rotated table - merge ink into one blob and grab its quad
        val k = Imgproc.getStructuringElement(Imgproc.MORPH_RECT, Size(15.0, 15.0))
        val blob = Mat()
        Imgproc.morphologyEx(th, blob, Imgproc.MORPH_CLOSE, k)
        return largestQuad(blob)
    }

    private fun quadArea(corners: Array<Point>): Double =
        Imgproc.contourArea(MatOfPoint(*corners))

    private fun dist(a: Point, b: Point) = hypot(a.x - b.x, a.y - b.y)

    private fun gridTiltDeg(c: Array<Point>): Double =
        (angleDeg(c[0], c[1]) + angleDeg(c[3], c[2])) / 2.0

    /** returns max(h_skew, v_skew) and aspect ratio. */
    private fun perspectiveMetrics(c: Array<Point>): Pair<Double, Double> {
        val top = dist(c[1], c[0]); val bottom = dist(c[2], c[3])
        val left = dist(c[3], c[0]); val right = dist(c[2], c[1])
        val hSkew = abs(top - bottom) / max(max(top, bottom), 1e-5)
        val vSkew = abs(left - right) / max(max(left, right), 1e-5)
        val aspect = ((top + bottom) / 2.0) / max((left + right) / 2.0, 1e-5)
        return Pair(max(hSkew, vSkew), aspect)
    }

    /** brightness, contrast, blownFrac, shadowRange inside the sheet quad. */
    private fun lightingMetrics(gray: Mat, corners: Array<Point>): DoubleArray {
        val mask = Mat.zeros(gray.size(), CvType.CV_8UC1)
        Imgproc.fillPoly(mask, listOf(MatOfPoint(*corners)), Scalar(255.0))
        val mean = MatOfDoubleWrap()
        val std = MatOfDoubleWrap()
        Core.meanStdDev(gray, mean.mat, std.mat, mask)
        val brightness = mean.value()
        val contrast = std.value()

        val total = Core.countNonZero(mask).coerceAtLeast(1)
        val blown = Mat()
        Core.inRange(gray, Scalar(250.0), Scalar(255.0), blown)
        Core.bitwise_and(blown, mask, blown)
        val blownFrac = Core.countNonZero(blown).toDouble() / total

        val r = Imgproc.boundingRect(MatOfPoint(*corners))
        val x2 = r.x + r.width / 2; val y2 = r.y + r.height / 2
        val quadRects = listOf(
            Rect(r.x, r.y, r.width / 2, r.height / 2),
            Rect(x2, r.y, r.x + r.width - x2, r.height / 2),
            Rect(r.x, y2, r.width / 2, r.y + r.height - y2),
            Rect(x2, y2, r.x + r.width - x2, r.y + r.height - y2),
        )
        val means = ArrayList<Double>()
        for (qr in quadRects) {
            val safe = Rect(
                qr.x.coerceIn(0, gray.cols() - 1),
                qr.y.coerceIn(0, gray.rows() - 1),
                qr.width.coerceIn(1, gray.cols()),
                qr.height.coerceIn(1, gray.rows())
            )
            if (safe.x + safe.width > gray.cols() || safe.y + safe.height > gray.rows()) continue
            val m = Core.mean(gray.submat(safe), mask.submat(safe))
            if (m.`val`[0] > 0) means.add(m.`val`[0])
        }
        val shadowRange = if (means.size >= 2) means.max() - means.min() else 0.0
        return doubleArrayOf(brightness, contrast, blownFrac, shadowRange)
    }

    private fun laplacianVar(gray: Mat): Double {
        val lap = Mat()
        Imgproc.Laplacian(gray, lap, CvType.CV_64F)
        val std = MatOfDoubleWrap()
        Core.meanStdDev(lap, MatOfDoubleWrap().mat, std.mat)
        val s = std.value()
        return s * s
    }

    private fun countBubbles(roi: Mat): Int {
        if (roi.rows() < 40 || roi.cols() < 40) return 0
        val blur = Mat(); Imgproc.GaussianBlur(roi, blur, Size(5.0, 5.0), 0.0)
        val th = Mat()
        Imgproc.adaptiveThreshold(
            blur, th, 255.0, Imgproc.ADAPTIVE_THRESH_GAUSSIAN_C,
            Imgproc.THRESH_BINARY_INV, 21, 8.0
        )
        val cnts = ArrayList<MatOfPoint>()
        Imgproc.findContours(th, cnts, Mat(), Imgproc.RETR_LIST, Imgproc.CHAIN_APPROX_SIMPLE)
        val approxCell = min(roi.cols() / 10.0, roi.rows() / 21.0)
        val rLo = approxCell * 0.18; val rHi = approxCell * 0.42
        var n = 0
        for (c in cnts) {
            val area = Imgproc.contourArea(c)
            if (area < 20) continue
            val radius = FloatArray(1)
            Imgproc.minEnclosingCircle(MatOfPoint2f(*c.toArray()), Point(), radius)
            val rad = radius[0].toDouble()
            if (rad in rLo..rHi) {
                val circ = area / (Math.PI * rad * rad + 1e-5)
                if (circ >= 0.45) n++
            }
        }
        return n
    }

    fun check(grayInput: Mat, tracker: StabilityTracker?): Result {
        // higher-res copy (cap 1280) for template check; 640 copy for the rest
        val maxDim = max(grayInput.rows(), grayInput.cols())
        val scaleHi = min(1.0, 1280.0 / maxDim)
        val grayHi = Mat()
        if (scaleHi < 1) Imgproc.resize(grayInput, grayHi, Size(grayInput.cols() * scaleHi, grayInput.rows() * scaleHi))
        else grayInput.copyTo(grayHi)

        val scale = 640.0 / max(grayHi.rows(), grayHi.cols())
        val gray = Mat()
        if (scale < 1) Imgproc.resize(grayHi, gray, Size(grayHi.cols() * scale, grayHi.rows() * scale))
        else grayHi.copyTo(gray)

        val fw = gray.cols(); val fh = gray.rows()
        val frameArea = (fw * fh).toDouble()
        val frameDiag = hypot(fw.toDouble(), fh.toDouble())

        val messages = ArrayList<String>()
        var penalties = 0.0
        var hardFail = false

        fun reject(msg: String, penalty: Double = 25.0) {
            if (msg !in messages) messages.add(msg)
            penalties += penalty; hardFail = true
        }
        fun warn(msg: String, penalty: Double = 4.0) {
            if (msg !in messages) messages.add(msg)
            penalties += penalty
        }

        var tilt = 0.0; var dx = 0.0; var dy = 0.0

        val blur = laplacianVar(gray)
        if (blur < BLUR_MIN) { reject("Image Blurry"); messages.add("Hold Steady") }

        val corners = detectGridQuad(gray)
        if (corners == null) {
            reject("Entire Sheet Not Visible", 60.0)
            messages.add("Align Sheet")
            tracker?.update(false, null, 0.0, 0.0, blur, frameDiag)
            return result(false, penalties, messages, 0, tilt, dx, dy)
        }

        val qArea = quadArea(corners)
        val coverage = qArea / frameArea

        val mx = EDGE_MARGIN_FRAC * fw; val my = EDGE_MARGIN_FRAC * fh
        val xs = corners.map { it.x }; val ys = corners.map { it.y }
        if (xs.min() < mx || ys.min() < my || xs.max() > fw - mx || ys.max() > fh - my) {
            reject("Sheet Cropped"); messages.add("Move Away")
        }
        if (coverage < COVERAGE_MIN) reject("Move Closer")
        else if (coverage > COVERAGE_MAX) reject("Move Away")

        tilt = gridTiltDeg(corners)
        if (abs(tilt) > TILT_MAX_DEG) reject(if (tilt > 0) "Rotate Left" else "Rotate Right")

        val (skew, aspect) = perspectiveMetrics(corners)
        if (skew > PERSPECTIVE_MAX) reject("Align Sheet")
        if (abs(aspect - EXPECTED_GRID_ASPECT) / EXPECTED_GRID_ASPECT > ASPECT_TOLERANCE) reject("Align Sheet")

        val gcx = xs.average(); val gcy = ys.average()
        dx = (gcx - fw / 2.0) / fw; dy = (gcy - fh / 2.0) / fh
        if (abs(dx) > CENTER_OFFSET_MAX) reject(if (dx < 0) "Move Right" else "Move Left", 15.0)
        if (abs(dy) > CENTER_OFFSET_MAX) reject(if (dy < 0) "Move Down" else "Move Up", 15.0)

        val light = lightingMetrics(gray, corners)
        val brightness = light[0]; val contrast = light[1]
        val blownFrac = light[2]; val shadowRange = light[3]
        if (brightness < BRIGHTNESS_MIN) reject("Too Dark")
        else if (brightness > BRIGHTNESS_MAX) reject("Too Bright")
        if (blownFrac > REFLECTION_BLOWN_FRAC) reject("Reflection Detected")
        if (shadowRange > SHADOW_RANGE_MAX) reject("Shadow Detected")
        if (contrast < CONTRAST_MIN) warn(if (brightness < 128) "Too Dark" else "Too Bright", 10.0)

        // template recognition on the higher-res copy
        val up = if (scale < 1) 1.0 / scale else 1.0
        val bx = Imgproc.boundingRect(MatOfPoint(*corners.map { Point(it.x * up, it.y * up) }.toTypedArray()))
        val safe = Rect(
            bx.x.coerceIn(0, grayHi.cols() - 1), bx.y.coerceIn(0, grayHi.rows() - 1),
            bx.width.coerceIn(1, grayHi.cols() - bx.x.coerceAtLeast(0)),
            bx.height.coerceIn(1, grayHi.rows() - bx.y.coerceAtLeast(0))
        )
        val nBubbles = if (safe.width > 1 && safe.height > 1) countBubbles(grayHi.submat(safe)) else 0
        if (nBubbles < 80) { reject("Align Sheet", 30.0); messages.add("Entire Sheet Not Visible") }

        val frameOk = !hardFail
        var stable = 0
        if (tracker != null) {
            stable = tracker.update(frameOk, Point(gcx, gcy), qArea, tilt, blur, frameDiag)
        }

        if (frameOk && stable < STABLE_FRAMES_REQUIRED) {
            messages.add("Hold Steady")
            penalties += max(0.0, 1.9 * (1 - stable.toDouble() / STABLE_FRAMES_REQUIRED))
        }

        val confidence = max(0.0, 100.0 - penalties)
        val isReady = frameOk && stable >= STABLE_FRAMES_REQUIRED && confidence >= CONFIDENCE_REQUIRED
        val outMsgs = if (isReady) listOf("Ready to Capture") else messages
        return result(isReady, penalties, outMsgs, stable, tilt, dx, dy)
    }

    private fun result(
        isReady: Boolean, penalties: Double, messages: List<String>,
        stability: Int, tilt: Double, dx: Double, dy: Double
    ) = Result(isReady, max(0.0, 100.0 - penalties), messages, stability, tilt, dx, dy)
}

/** Small helper wrapping a MatOfDouble for meanStdDev outputs. */
class MatOfDoubleWrap {
    val mat = org.opencv.core.MatOfDouble()
    fun value(): Double {
        val a = mat.toArray()
        return if (a.isEmpty()) 0.0 else a[0]
    }
}
