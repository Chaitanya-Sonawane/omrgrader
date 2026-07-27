# OMR Capture — Android + OpenCV (on-device)

A native Android app that uses the **phone camera** to capture an NMMS-style OMR
answer sheet and grade it **entirely on the device** with OpenCV — no server
required. It mirrors the real-time auto-capture pipeline of the Python backend
(`backend/frame_quality.py`, `backend/omr_engine.py`, `backend/scoring.py`),
re-implemented in Kotlin.

## What it does

- **Live camera preview** (CameraX) with per-frame quality validation running on
  the camera Y-plane (no JPEG decode on the hot path).
- **On-screen guidance** ("Move Closer", "Rotate Left", "Hold Steady", …), a
  0–100% confidence bar and a stability counter.
- **Hands-free auto-capture**: fires once the frame is stable for 20 consecutive
  checks with confidence ≥ 98% (toggleable). Manual **Capture** button too.
- **On-device processing**: perspective correction + deskew, shadow removal
  (background division) and CLAHE contrast enhancement.
- **On-device bubble detection & grading**: circle detection → 1-D k-means row/col
  clustering → per-row-normalized fill scoring → per-question answer resolution →
  scoring against an answer key you enter (with marks / negative marking).

## Project layout

```
android/
  settings.gradle.kts, build.gradle.kts, gradle.properties
  app/
    build.gradle.kts
    src/main/AndroidManifest.xml
    src/main/java/com/omr/capture/
      MainActivity.kt        CameraX preview, analysis loop, capture, UI dialogs
      FrameQuality.kt        per-frame validation (port of frame_quality.py)
      StabilityTracker.kt    20-frame stability gate
      OmrEngine.kt           deskew + bubble detection + fill scoring (port of omr_engine.py)
      Scoring.kt             answer-key comparison (port of scoring.py)
      AnswerKeyStore.kt      persist the answer key + marking scheme
    src/main/res/            layout, theme, launcher icon
```

## Build & run

Requirements: Android Studio (Giraffe or newer) or command-line Android SDK +
JDK 17. OpenCV is pulled automatically from Maven Central
(`org.opencv:opencv:4.11.0`) — nothing to download manually.

1. Open the `android/` folder in Android Studio (it will generate the Gradle
   wrapper and sync dependencies).
   - Command line alternative: from `android/`, run `gradle wrapper` once, then
     `./gradlew assembleDebug`.
2. Connect a **physical Android device** (recommended — camera + OpenCV are slow
   in emulators) with USB debugging enabled.
3. Run the app, grant the camera permission.
4. Tap **Answer Key**, paste the key (one per line, e.g. `1 2` = Q1 → option 2),
   set marks / negative marking, Save.
5. Point the camera at the sheet. Let it auto-capture (or tap **Capture**). The
   score and per-question breakdown appear in a dialog.

`minSdk` is 24, `targetSdk`/`compileSdk` 34.

## Calibration

The engine is calibrated for the NMMS template: 40 questions, 2 blocks of 20,
4 options, 20×8 bubble grid. For a different sheet, adjust `N_ROWS` / `N_COLS`
and the block layout in `OmrEngine.kt`, mirroring the Python `omr_engine.py`.

## Note on verification

This module was authored in an environment without the Android SDK, so it has
**not been compiled/instrumented here**. Open it in Android Studio to build and
run on a device; the logic is a faithful line-by-line port of the already-tested
Python pipeline.
