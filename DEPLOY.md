# Deployment

Repo: https://github.com/Chaitanya-Sonawane/omrgrader

Architecture: FastAPI **backend on Render**, static **frontend on Netlify**
(frontend calls the backend via `window.OMR_API_BASE` in `frontend/config.js`).

## 1. Backend on Render

### Recommended: Blueprint (uses render.yaml)

1. Go to https://dashboard.render.com → **New +** → **Blueprint**.
2. Connect your GitHub account and pick the `omrgrader` repo.
3. Render reads `render.yaml` and proposes the `omrgrader-backend` web service
   (Python, root dir `backend`, `pip install -r requirements.txt`,
   `uvicorn app:app --host 0.0.0.0 --port $PORT`, health check `/api/health`).
4. Click **Apply** and wait for the first deploy to go live.
5. Copy the service URL, e.g. `https://omrgrader-backend.onrender.com`.

### If you created a plain Web Service (build fails with "No such file: requirements.txt")

That error means Render is building from the **repo root**, not `backend/`.
Two ways to fix it:

- **Easiest:** In the service's **Settings**, set **Root Directory** = `backend`,
  keep **Build Command** = `pip install -r requirements.txt` and
  **Start Command** = `uvicorn app:app --host 0.0.0.0 --port $PORT`, then redeploy.
- **Or leave Root Directory empty** — the repo now also has root-level
  `requirements.txt`, `runtime.txt` and a `Procfile`, so a root build works too:
  - Build Command: `pip install -r requirements.txt`
  - Start Command: `cd backend && uvicorn app:app --host 0.0.0.0 --port $PORT`

## 2. Point the frontend at the backend

Edit `frontend/config.js` and set your Render URL (no trailing slash):

```js
window.OMR_API_BASE = "https://omrgrader-backend.onrender.com";
```

Commit and push — Netlify will redeploy automatically:

```bash
git add frontend/config.js && git commit -m "Point frontend at Render backend" && git push
```

## 3. Frontend on Netlify

CLI (after `netlify login`):

```bash
netlify init          # link this repo to a new Netlify site
netlify deploy --prod # publishes the "frontend" dir (see netlify.toml)
```

Or via the Netlify dashboard: **Add new site → Import from Git → pick `omrgrader`**.
`netlify.toml` already sets the publish directory to `frontend` with no build step.

## Python version

Render's default for new services is Python 3.14, whose fresh wheels can break
imports (uvicorn then reports `Could not import module "app"`). The repo pins
Python **3.11.9** so the tested wheels are used. Render honors these in this
order of precedence: `PYTHON_VERSION` env var (set in `render.yaml`) → the
`.python-version` file → `runtime.txt`. `runtime.txt` alone is no longer
reliably honored, so keep `.python-version` (and, for dashboard-created
services, set `PYTHON_VERSION=3.11.9`).

## Notes
- Backend CORS is open (`allow_origins=["*"]`), so the Netlify origin can call Render.
- Render's free tier sleeps when idle; the first request after idle is slow to wake.
- Camera capture needs HTTPS — both Render and Netlify provide it automatically.
