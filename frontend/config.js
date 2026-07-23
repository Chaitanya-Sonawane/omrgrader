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
window.OMR_API_BASE = "https://rigorously-robeless-alva.ngrok-free.dev";
