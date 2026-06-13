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


def _env(name: str, default: str = "") -> str:
    """Read an env var with a UTF-8 BOM and surrounding whitespace stripped.

    Some shells (notably PowerShell piping) prepend a BOM when setting env
    vars; stripping it here keeps provider selection and credentials robust.
    """
    return os.getenv(name, default).replace(chr(0xFEFF), "").strip()


def _parse_cors_origins(raw: str) -> List[str]:
    """Parse a comma-separated origins string into a clean, ordered list.

    Robust to a leading UTF-8 BOM and stray surrounding whitespace/quotes,
    which some shells (notably PowerShell piping) inject when setting env vars.
    """
    bom = chr(0xFEFF)
    origins = [
        origin.replace(bom, "").strip().strip("\"'")
        for origin in raw.replace(bom, "").split(",")
    ]
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
        self.llm_provider: str = _env("LLM_PROVIDER", "ollama").lower()
        self.ollama_base_url: str = _env("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.ollama_model: str = _env("OLLAMA_MODEL", "qwen2.5-coder:14b")
        self.gemini_api_key: str = _env("GEMINI_API_KEY")
        self.gemini_model: str = _env("GEMINI_MODEL", "gemini-2.0-flash")
        self.groq_api_key: str = _env("GROQ_API_KEY")
        self.groq_model: str = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.openrouter_api_key: str = _env("OPENROUTER_API_KEY")
        self.openrouter_model: str = _env(
            "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"
        )
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
