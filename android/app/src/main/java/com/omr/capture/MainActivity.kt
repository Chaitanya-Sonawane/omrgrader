package com.omr.capture

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.util.Log
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import com.omr.capture.databinding.ActivityMainBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.opencv.android.OpenCVLoader
import org.opencv.core.Core
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.MatOfByte
import org.opencv.imgcodecs.Imgcodecs
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var keyStore: AnswerKeyStore

    private var imageCapture: ImageCapture? = null
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val scope = CoroutineScope(Dispatchers.Main)

    private val tracker = StabilityTracker()
    private val capturing = AtomicBoolean(false)
    private var openCvReady = false

    private val requestPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startCamera() else {
            Toast.makeText(this, "Camera permission is required", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        keyStore = AnswerKeyStore(this)

        openCvReady = OpenCVLoader.initLocal()
        if (!openCvReady) {
            Toast.makeText(this, "OpenCV failed to load", Toast.LENGTH_LONG).show()
        }

        binding.captureButton.setOnClickListener { triggerCapture() }
        binding.answerKeyButton.setOnClickListener { showAnswerKeyDialog() }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) startCamera() else requestPermission.launch(Manifest.permission.CAMERA)
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()

            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.previewView.surfaceProvider)
            }

            imageCapture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                .build()

            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build().also {
                    it.setAnalyzer(analysisExecutor, ::analyzeFrame)
                }

            provider.unbindAll()
            provider.bindToLifecycle(
                this, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture, analysis
            )
        }, ContextCompat.getMainExecutor(this))
    }

    /** Runs on the analysis executor for every preview frame. */
    private fun analyzeFrame(image: ImageProxy) {
        if (!openCvReady || capturing.get()) { image.close(); return }
        try {
            val gray = yPlaneToGray(image)
            val result = FrameQuality.check(gray, tracker)
            runOnUiThread { renderGuidance(result) }

            if (result.isReady && binding.autoCaptureCheck.isChecked) {
                runOnUiThread { triggerCapture() }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "frame analysis failed", t)
        } finally {
            image.close()
        }
    }

    private fun renderGuidance(r: FrameQuality.Result) {
        binding.guidanceText.text = r.messages.joinToString("  •  ").ifBlank { "Hold Steady" }
        binding.confidenceBar.progress = r.confidence.toInt()
        binding.confidenceText.text = "${r.confidence.toInt()}%  (stable ${r.stability}/${FrameQuality.STABLE_FRAMES_REQUIRED})"
        binding.guidanceText.setTextColor(if (r.isReady) Color.GREEN else Color.WHITE)
    }

    private fun triggerCapture() {
        val capture = imageCapture ?: return
        if (!capturing.compareAndSet(false, true)) return
        binding.guidanceText.text = "Capturing…"

        capture.takePicture(
            ContextCompat.getMainExecutor(this),
            object : ImageCapture.OnImageCapturedCallback() {
                override fun onCaptureSuccess(image: ImageProxy) {
                    val rotation = image.imageInfo.rotationDegrees
                    val bytes = jpegBytes(image)
                    image.close()
                    processCaptured(bytes, rotation)
                }

                override fun onError(exc: ImageCaptureException) {
                    Log.e(TAG, "capture failed", exc)
                    Toast.makeText(this@MainActivity, "Capture failed: ${exc.message}", Toast.LENGTH_SHORT).show()
                    finishCapture()
                }
            }
        )
    }

    private fun processCaptured(jpeg: ByteArray, rotationDegrees: Int) {
        scope.launch {
            val res = withContext(Dispatchers.Default) {
                var bgr = Imgcodecs.imdecode(MatOfByte(*jpeg), Imgcodecs.IMREAD_COLOR)
                bgr = applyRotation(bgr, rotationDegrees)
                val cleaned = OmrEngine.process(bgr)
                OmrEngine.scan(cleaned)
            }
            showResult(res)
            finishCapture()
        }
    }

    private fun showResult(scan: OmrEngine.ScanResult) {
        val key = keyStore.getKey()
        val sb = StringBuilder()
        if (key.isEmpty()) {
            sb.append("No answer key set — showing detected answers only.\n\n")
            scan.answers.toSortedMap().forEach { (q, b) ->
                sb.append("Q$q: ${b.selected ?: b.flagReason}\n")
            }
        } else {
            val scored = Scoring.score(
                scan.answers, key, keyStore.marksPerCorrect, keyStore.negativeMarking
            )
            sb.append("Score: ${scored.totalMarks}/${scored.maxMarks}  (${scored.percentage}%)  Grade ${Scoring.grade(scored.percentage)}\n")
            sb.append("Correct ${scored.correct} | Wrong ${scored.wrong} | Blank ${scored.blank} | Multiple ${scored.multiple} | N/D ${scored.notDetected}\n\n")
            scored.questionResults.forEach {
                sb.append("Q${it.question}: you=${it.studentAnswer ?: "-"} key=${it.correctAnswer} [${it.status}]\n")
            }
        }
        sb.append("\nBubbles detected: ${scan.bubblesDetected} | sharpness ${scan.blurScore}")
        if (scan.warnings.isNotEmpty()) sb.append("\n⚠ ${scan.warnings.joinToString("; ")}")

        AlertDialog.Builder(this)
            .setTitle("Scan result")
            .setMessage(sb.toString())
            .setPositiveButton("Done", null)
            .show()
    }

    private fun finishCapture() {
        tracker.reset()
        capturing.set(false)
    }

    private fun showAnswerKeyDialog() {
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 0)
        }
        val info = TextView(this).apply {
            text = "Paste key, one per line: \"1 2\" means Q1 -> option 2."
        }
        val input = EditText(this).apply {
            setText(keyStore.getKey().toSortedMap().entries.joinToString("\n") { "${it.key} ${it.value}" })
            minLines = 6
        }
        val marks = EditText(this).apply {
            hint = "Marks per correct"; setText(keyStore.marksPerCorrect.toString())
        }
        val neg = EditText(this).apply {
            hint = "Negative marking"; setText(keyStore.negativeMarking.toString())
        }
        container.addView(info); container.addView(input)
        container.addView(marks); container.addView(neg)

        AlertDialog.Builder(this)
            .setTitle("Answer Key")
            .setView(container)
            .setPositiveButton("Save") { _, _ ->
                keyStore.setKey(keyStore.parseBulk(input.text.toString()))
                keyStore.marksPerCorrect = marks.text.toString().toDoubleOrNull() ?: 1.0
                keyStore.negativeMarking = neg.text.toString().toDoubleOrNull() ?: 0.0
                Toast.makeText(this, "Answer key saved", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ---- image conversion helpers ----

    /** Copy the camera Y plane into a single-channel grayscale Mat. */
    private fun yPlaneToGray(image: ImageProxy): Mat {
        val plane = image.planes[0]
        val buffer = plane.buffer
        val rowStride = plane.rowStride
        val width = image.width
        val height = image.height
        val gray = Mat(height, width, CvType.CV_8UC1)
        val row = ByteArray(width)
        for (y in 0 until height) {
            buffer.position(y * rowStride)
            buffer.get(row, 0, width)
            gray.put(y, 0, row)
        }
        return gray
    }

    private fun jpegBytes(image: ImageProxy): ByteArray {
        val buffer = image.planes[0].buffer
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)
        return bytes
    }

    private fun applyRotation(bgr: Mat, degrees: Int): Mat {
        val code = when (((degrees % 360) + 360) % 360) {
            90 -> Core.ROTATE_90_CLOCKWISE
            180 -> Core.ROTATE_180
            270 -> Core.ROTATE_90_COUNTERCLOCKWISE
            else -> return bgr
        }
        val out = Mat()
        Core.rotate(bgr, out, code)
        return out
    }

    override fun onDestroy() {
        super.onDestroy()
        analysisExecutor.shutdown()
    }

    companion object {
        private const val TAG = "OMRCapture"
    }
}
