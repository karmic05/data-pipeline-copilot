"""Uvicorn entrypoint: uvicorn main:app --reload --port 8000"""
from app.api import app  # noqa: F401
