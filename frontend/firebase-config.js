// ---------------------------------------------------------------------------
// Firebase configuration for the OMR Grader persistence layer.
//
// The app works in an OPTIMISTIC-FIRST way: every scan, score and student
// assignment is stored INSTANTLY in a local cache (IndexedDB/localStorage) and
// the UI never waits for the network. If the Firebase config below is filled
// in with a real project, the local data is additionally synced to Firebase
// Firestore (with offline persistence) and uploaded Excel/PDF files can go to
// Firebase Storage. If it is left as-is (placeholder), the app still works
// fully — it just persists locally on this device only.
//
// HOW TO ENABLE FIREBASE (takes ~2 minutes):
//   1. Go to https://console.firebase.google.com and create a project.
//   2. Add a "Web app" and copy the firebaseConfig values it shows you.
//   3. Paste those values below (replace every "YOUR_..." placeholder).
//   4. In the console enable: Firestore Database (production or test mode) and,
//      optionally, Storage.
//
// NOTE: these Web API keys are NOT secrets — they are meant to ship in the
// browser. Access is controlled by Firestore/Storage security rules, not by
// hiding this key.
// ---------------------------------------------------------------------------
window.OMR_FIREBASE_CONFIG = {
  apiKey:            "AIzaSyCRGPOF_HLkqckodWxnkCdT7CPW9wqHbtU",
  authDomain:        "omr-scanner-5a3b4.firebaseapp.com",
  projectId:         "omr-scanner-5a3b4",
  storageBucket:     "omr-scanner-5a3b4.firebasestorage.app",
  messagingSenderId: "535220435667",
  appId:             "1:535220435667:web:daa49cd8e897f9c3afb226",
  measurementId:     "G-YP5VMZYMH6",
};

// Returns true only when the config above has been filled with real values
// (i.e. it no longer contains the "YOUR_..." placeholders). Used by the
// persistence layer to decide whether to connect to Firebase or stay
// local-only.
window.OMR_FIREBASE_ENABLED = (function(){
  try{
    var c = window.OMR_FIREBASE_CONFIG || {};
    return !!(c.apiKey && c.projectId &&
      c.apiKey.indexOf("YOUR_") === -1 &&
      c.projectId.indexOf("YOUR_") === -1);
  }catch(_){ return false; }
})();
