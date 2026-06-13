"""Application settings loaded from ``backend/.env`` via python-dotenv.

Import-time side effect: ``load_dotenv`` is called once against the backend
directory (``override=False`` so real environment variables always win).
A module-level :data:`settings` instance is the single source of truth for
provider credentials, model names and CORS configuration.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

#: backend/ directory (this file lives at backend/app/config.py).
BACKEND_DIR: Path = Path(__file__).resolve().parent.parent

load_dotenv(BACKEND_DIR / ".env", override=False)

_DEFAULT_CORS_ORIGINS = "http://localhost:3000"


def _parse_cors_origins(raw: str) -> List[str]:
    """Parse a comma-separated origins string into a clean, ordered list."""
    origins = [origin.strip() for origin in raw.split(",")]
    cleaned = [origin for origin in origins if origin]
    if not cleaned:
        logger.warning(
            "CORS_ORIGINS is empty or malformed (%r); falling back to %s",
            raw,
            _DEFAULT_CORS_ORIGINS,
        )
        return [_DEFAULT_CORS_ORIGINS]
    return cleaned


class Settings:
    """Dataclass-style settings snapshot read from the environment.

    Attributes
    ----------
    llm_provider:
        One of ``ollama | gemini | groq | openrouter``.
    ollama_base_url / ollama_model:
        Local Ollama OpenAI-compatible endpoint configuration.
    gemini_api_key / gemini_model:
        Google Gemini configuration.
    groq_api_key / groq_model:
        Groq configuration.
    openrouter_api_key / openrouter_model:
        OpenRouter configuration.
    cors_origins:
        Allowed browser origins, parsed from comma-separated ``CORS_ORIGINS``.
    """

    def __init__(self) -> None:
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
        self.ollama_base_url: str = os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434/v1"
        ).strip()
        self.ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b").strip()
        self.gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
        self.gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
        self.groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_model: str = os.getenv(
            "GROQ_MODEL", "llama-3.3-70b-versatile"
        ).strip()
        self.openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.openrouter_model: str = os.getenv(
            "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"
        ).strip()
        self.cors_origins: List[str] = _parse_cors_origins(
            os.getenv("CORS_ORIGINS", _DEFAULT_CORS_ORIGINS)
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return (
            f"Settings(llm_provider={self.llm_provider!r}, "
            f"cors_origins={self.cors_origins!r})"
        )


#: Module-level singleton consumed across the backend.
settings = Settings()
