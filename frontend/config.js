// Frontend runtime config.
//
// API base resolution (see index.html -> getApiBase):
//   1. The backend normally serves this very page, so the app first tries the
//      SAME ORIGIN it was loaded from. This makes local development
//      (uvicorn app:app --port 8000 -> http://localhost:8000) work out of the
//      box with NO configuration and no "Failed to fetch" errors.
//   2. Only when the same origin has no backend (e.g. the frontend is hosted on
//      a static host like Netlify while the API runs on Render) does the app
//      fall back to the URL below.
//
// So this value is a FALLBACK for split hosting only. Set it to your live
// backend base URL WITHOUT a trailing slash, or leave it "" for same-origin only.
//
//   window.OMR_API_BASE = "https://omrgrader-backend.onrender.com";
//
// NOTE: This must be a live, permanent backend URL. Temporary tunnels (e.g.
// ngrok free-tier URLs) expire and cause "Cannot reach the scanning server"
// errors once the tunnel is gone. This points at the live Render service that
// actually serves the API (verified: https://omrgrader.onrender.com/api/health
// returns {"status":"ok"} from uvicorn). The older "omrgrader-backend" host is
// dead (Render "no-server"). If you deploy the backend elsewhere, update this
// value to your own live base URL (no trailing slash).
window.OMR_API_BASE = "https://omrgrader.onrender.com";

// OPTIONAL failover list. To make the app resilient against a single backend
// URL going dead again, list every backend host you have here (no trailing
// slash). On startup the app probes same-origin first, then each candidate
// below in order, and automatically uses the first one whose /api/health
// answers. This means if the primary URL ever changes/dies, the app keeps
// working as long as ANY listed host is live - no code edit + redeploy needed.
// Primary/fallback order: the app probes these top-to-bottom and uses the first
// that answers /api/health. To make Hugging Face Spaces (Docker) the PRIMARY
// backend with Render as the fallback, uncomment the HF line and replace
// <your-username> with your actual HF username (the Space URL is shown as
// "Running" on the Space page, e.g. https://<user>-omr-grader.hf.space).
window.OMR_API_BASE_CANDIDATES = [
  // "https://<your-username>-omr-grader.hf.space", // Hugging Face Spaces (primary) - uncomment & set username
  "https://omrgrader.onrender.com",                 // Render (fallback)
];
