"""Root-level ASGI entry point.

Render (and other hosts) may run ``uvicorn app:app`` from the repository root
instead of the ``backend/`` directory. The real application lives in
``backend/app.py``. This shim makes ``app:app`` importable from the repo root by
adding ``backend/`` to ``sys.path`` and loading the FastAPI instance from
``backend/app.py`` (loaded under a distinct module name to avoid colliding with
this file, which itself is imported as the ``app`` module).
"""
import importlib.util
import os
import sys

_BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_spec = importlib.util.spec_from_file_location(
    "backend_app", os.path.join(_BACKEND_DIR, "app.py")
)
_backend_app = importlib.util.module_from_spec(_spec)
sys.modules["backend_app"] = _backend_app
_spec.loader.exec_module(_backend_app)

app = _backend_app.app
