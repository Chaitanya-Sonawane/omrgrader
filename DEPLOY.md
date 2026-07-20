# Deployment

Repo: https://github.com/Chaitanya-Sonawane/omrgrader

Architecture: FastAPI **backend on Render**, static **frontend on Netlify**
(frontend calls the backend via `window.OMR_API_BASE` in `frontend/config.js`).

## 1. Backend on Render (via render.yaml blueprint)

1. Go to https://dashboard.render.com → **New +** → **Blueprint**.
2. Connect your GitHub account and pick the `omrgrader` repo.
3. Render reads `render.yaml` and proposes the `omrgrader-backend` web service
   (Python, root dir `backend`, `pip install -r requirements.txt`,
   `uvicorn app:app --host 0.0.0.0 --port $PORT`, health check `/api/health`).
4. Click **Apply** and wait for the first deploy to go live.
5. Copy the service URL, e.g. `https://omrgrader-backend.onrender.com`.

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

## Notes
- Backend CORS is open (`allow_origins=["*"]`), so the Netlify origin can call Render.
- Render's free tier sleeps when idle; the first request after idle is slow to wake.
- Camera capture needs HTTPS — both Render and Netlify provide it automatically.
