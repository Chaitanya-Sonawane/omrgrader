package com.omr.capture

import org.opencv.core.Point
import kotlin.math.abs
import kotlin.math.hypot

/**
 * Counts consecutive frames where position, angle, scale and blur are all
 * stable AND the frame itself passed every quality gate. Any movement or a
 * bad frame resets the counter. On-device port of `StabilityTracker` from
 * `frame_quality.py`.
 */
class StabilityTracker {

    private data class Snapshot(val center: Point?, val area: Double, val tilt: Double, val blur: Double)

    var stableCount: Int = 0
        private set

    private var last: Snapshot? = null
    private var lastSeen: Long = System.currentTimeMillis()

    fun reset() {
        stableCount = 0
        last = null
    }

    fun update(frameOk: Boolean, center: Point?, area: Double, tilt: Double, blur: Double, frameDiag: Double): Int {
        val now = System.currentTimeMillis()
        if (now - lastSeen > GAP_MS) {
            stableCount = 0
            last = null
        }
        lastSeen = now

        val snap = Snapshot(center, area, tilt, blur)
        if (!frameOk || center == null) {
            stableCount = 0
            last = if (center != null) snap else null
            return 0
        }

        val prev = last
        if (prev?.center != null) {
            val moved = (
                hypot(center.x - prev.center.x, center.y - prev.center.y) > POS_TOL * frameDiag ||
                abs(area - prev.area) / maxOf(prev.area, 1e-5) > AREA_TOL ||
                abs(tilt - prev.tilt) > TILT_TOL ||
                abs(blur - prev.blur) / maxOf(prev.blur, 1e-5) > BLUR_TOL
            )
            stableCount = if (moved) 0 else stableCount + 1
        } else {
            stableCount = 0
        }
        last = snap
        return stableCount
    }

    companion object {
        private const val GAP_MS = 1500L
        private const val POS_TOL = 0.015
        private const val AREA_TOL = 0.06
        private const val TILT_TOL = 1.0
        private const val BLUR_TOL = 0.45
    }
}
