// Frontend runtime config.
//
// When the frontend is hosted separately from the backend (e.g. on Netlify while
// the FastAPI backend runs on Render), set this to your Render backend base URL,
// WITHOUT a trailing slash, e.g.:
//
//   window.OMR_API_BASE = "https://omrgrader-backend.onrender.com";
//
// Leave it empty ("") to call the backend on the same origin (e.g. when the
// backend itself serves this page).
window.OMR_API_BASE = "https://omrgrader-backend.onrender.com";
