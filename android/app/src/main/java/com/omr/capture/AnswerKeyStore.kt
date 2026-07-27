package com.omr.capture

import android.content.Context

/**
 * Persists the answer key (question -> option 1-4) plus marking scheme in
 * SharedPreferences, so the on-device grader can score captured sheets.
 */
class AnswerKeyStore(context: Context) {

    private val prefs = context.getSharedPreferences("omr_answer_key", Context.MODE_PRIVATE)

    var marksPerCorrect: Double
        get() = prefs.getFloat("marks_per_correct", 1.0f).toDouble()
        set(v) = prefs.edit().putFloat("marks_per_correct", v.toFloat()).apply()

    var negativeMarking: Double
        get() = prefs.getFloat("negative_marking", 0.0f).toDouble()
        set(v) = prefs.edit().putFloat("negative_marking", v.toFloat()).apply()

    /** Stored as "q:opt" pairs joined by commas, e.g. "1:2,2:4,3:1". */
    fun getKey(): Map<Int, Int> {
        val raw = prefs.getString("key", "") ?: ""
        if (raw.isBlank()) return emptyMap()
        return raw.split(",").mapNotNull {
            val parts = it.split(":")
            val q = parts.getOrNull(0)?.trim()?.toIntOrNull()
            val opt = parts.getOrNull(1)?.trim()?.toIntOrNull()
            if (q != null && opt != null) q to opt else null
        }.toMap()
    }

    fun setKey(key: Map<Int, Int>) {
        val raw = key.toSortedMap().entries.joinToString(",") { "${it.key}:${it.value}" }
        prefs.edit().putString("key", raw).apply()
    }

    /**
     * Parse bulk text like "1 2\n2 4\n3 1" or "1:2,2:4" into a key map.
     * Accepts space, tab, colon or comma separators between question and option.
     */
    fun parseBulk(text: String): Map<Int, Int> {
        val key = LinkedHashMap<Int, Int>()
        for (line in text.split("\n", ",")) {
            val nums = line.trim().split(Regex("[\\s:]+")).mapNotNull { it.trim().toIntOrNull() }
            if (nums.size >= 2) key[nums[0]] = nums[1]
        }
        return key
    }
}
