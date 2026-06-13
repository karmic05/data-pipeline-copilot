"""Vercel Python entrypoint.

Vercel's Python runtime detects an ASGI app named ``app`` in ``api/*.py`` and
serves it as a Function. ``backend/vercel.json`` rewrites every path to this
file so the whole FastAPI app is reachable. For local/container runs use
``main:app`` instead (see the repo's Dockerfile / README).
"""
from app.api import app  # noqa: F401
