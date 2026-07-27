package com.omr.capture

/** On-device port of `scoring.py`: compares detected answers with a key. */
object Scoring {

    data class QuestionResult(
        val question: Int,
        val correctAnswer: Int?,
        val studentAnswer: Int?,
        val status: String,   // correct | wrong | blank | multiple | not_detected
        val marks: Double,
    )

    data class StudentResult(
        val totalQuestions: Int,
        var correct: Int = 0,
        var wrong: Int = 0,
        var blank: Int = 0,
        var multiple: Int = 0,
        var notDetected: Int = 0,
        var totalMarks: Double = 0.0,
        var maxMarks: Double = 0.0,
        var percentage: Double = 0.0,
        val questionResults: MutableList<QuestionResult> = mutableListOf(),
    )

    fun score(
        scanAnswers: Map<Int, OmrEngine.BubbleResult>,
        answerKey: Map<Int, Int>,
        marksPerCorrect: Double = 1.0,
        negativeMarking: Double = 0.0,
    ): StudentResult {
        val result = StudentResult(
            totalQuestions = answerKey.size,
            maxMarks = answerKey.size * marksPerCorrect,
        )

        for (q in answerKey.keys.sorted()) {
            val correctOpt = answerKey[q]
            val bubble = scanAnswers[q]

            val (status, marks) = when {
                bubble == null || (bubble.flagged && bubble.flagReason == "not detected") -> {
                    result.notDetected++; Pair("not_detected", 0.0)
                }
                bubble.isMultiple -> { result.multiple++; Pair("multiple", -negativeMarking) }
                bubble.selected == null -> { result.blank++; Pair("blank", 0.0) }
                bubble.selected == correctOpt -> { result.correct++; Pair("correct", marksPerCorrect) }
                else -> { result.wrong++; Pair("wrong", -negativeMarking) }
            }

            result.totalMarks += marks
            result.questionResults.add(
                QuestionResult(q, correctOpt, bubble?.selected, status, marks)
            )
        }

        result.totalMarks = maxOf(0.0, round2(result.totalMarks))
        result.percentage = round2(
            if (result.maxMarks > 0) result.totalMarks / result.maxMarks * 100 else 0.0
        )
        return result
    }

    fun grade(pct: Double): String = when {
        pct >= 90 -> "A+"; pct >= 80 -> "A"; pct >= 70 -> "B+"
        pct >= 60 -> "B"; pct >= 50 -> "C"; pct >= 40 -> "D"; else -> "F"
    }

    private fun round2(v: Double) = Math.round(v * 100.0) / 100.0
}
