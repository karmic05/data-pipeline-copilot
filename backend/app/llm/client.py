"""LLM provider abstraction over the OpenAI-compatible chat-completions API.

All four supported providers (Ollama, Google Gemini, Groq, OpenRouter) expose
OpenAI-compatible endpoints, so a single :class:`openai.AsyncOpenAI` client
covers them all. Provider selection and credentials come from the environment
(see ``backend/.env.example``). ``app.config.settings`` is used when
importable, with a direct ``os.environ`` fallback so this module never
hard-depends on the config module.

The LLM layer only ever receives structured IR/report JSON - never raw source
code - and degrades gracefully: every connection/auth failure surfaces as
:class:`LLMUnavailable` so callers can fall back to deterministic output.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx
import openai
from openai import AsyncOpenAI

from app.schemas.report import ProviderInfo

logger = logging.getLogger(__name__)

PROVIDERS: Tuple[str, ...] = ("ollama", "gemini", "groq", "openrouter")

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_DEFAULT_MODELS: Dict[str, str] = {
    "ollama": "qwen2.5-coder:14b",
    "gemini": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
}

_KEY_HELP: Dict[str, str] = {
    "gemini": "Get a free key at https://aistudio.google.com and set GEMINI_API_KEY.",
    "groq": "Get a free key at https://console.groq.com and set GROQ_API_KEY.",
    "openrouter": "Get a free key at https://openrouter.ai and set OPENROUTER_API_KEY.",
}

_STATUS_TTL_SECONDS = 30.0
_OLLAMA_PROBE_TIMEOUT_S = 2.0

# provider name -> (time.monotonic() at probe time, probe result)
_status_cache: Dict[str, Tuple[float, ProviderInfo]] = {}


class LLMUnavailable(Exception):
    """Raised when the configured LLM provider cannot serve a completion."""


@dataclass(frozen=True)
class _ProviderConfig:
    """Resolved connection settings for one provider."""

    name: str
    base_url: str
    api_key: str
    model: str
    key_env: Optional[str]
    key_help: str


def _setting(attr: str, env_name: str, default: str = "") -> str:
    """Read a config value from ``app.config.settings`` or the environment.

    ``app.config`` is owned by another module and may not exist yet; the env
    var names themselves are the contract, so fall back to ``os.environ``.
    """
    try:
        from app.config import settings  # type: ignore[import-not-found]

        value = getattr(settings, attr, None)
        if value:
            return str(value)
    except ImportError:
        pass
    return os.environ.get(env_name, "").strip() or default


def _provider_name() -> str:
    """The active provider from ``LLM_PROVIDER`` (defaults to ``ollama``)."""
    name = _setting("llm_provider", "LLM_PROVIDER", "ollama").strip().lower()
    if name not in PROVIDERS:
        logger.warning("Unknown LLM_PROVIDER %r; falling back to 'ollama'", name)
        return "ollama"
    return name


def _resolve(name: str) -> _ProviderConfig:
    """Resolve base URL, API key and model for the given provider."""
    if name == "ollama":
        model = _setting("ollama_model", "OLLAMA_MODEL", _DEFAULT_MODELS["ollama"])
        return _ProviderConfig(
            name="ollama",
            base_url=_setting("ollama_base_url", "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
            api_key="ollama",
            model=model,
            key_env=None,
            key_help=(
                "Install Ollama from https://ollama.com, run 'ollama serve', "
                f"then 'ollama pull {model}'."
            ),
        )
    if name == "gemini":
        return _ProviderConfig(
            name="gemini",
            base_url=GEMINI_BASE_URL,
            api_key=_setting("gemini_api_key", "GEMINI_API_KEY"),
            model=_setting("gemini_model", "GEMINI_MODEL", _DEFAULT_MODELS["gemini"]),
            key_env="GEMINI_API_KEY",
            key_help=_KEY_HELP["gemini"],
        )
    if name == "groq":
        return _ProviderConfig(
            name="groq",
            base_url=GROQ_BASE_URL,
            api_key=_setting("groq_api_key", "GROQ_API_KEY"),
            model=_setting("groq_model", "GROQ_MODEL", _DEFAULT_MODELS["groq"]),
            key_env="GROQ_API_KEY",
            key_help=_KEY_HELP["groq"],
        )
    if name == "openrouter":
        return _ProviderConfig(
            name="openrouter",
            base_url=OPENROUTER_BASE_URL,
            api_key=_setting("openrouter_api_key", "OPENROUTER_API_KEY"),
            model=_setting("openrouter_model", "OPENROUTER_MODEL", _DEFAULT_MODELS["openrouter"]),
            key_env="OPENROUTER_API_KEY",
            key_help=_KEY_HELP["openrouter"],
        )
    raise ValueError(f"Unknown LLM provider: {name!r}")


def _model_matches(wanted: str, pulled: str) -> bool:
    """Whether a pulled Ollama model name satisfies the configured model."""
    if pulled == wanted:
        return True
    if pulled == f"{wanted}:latest":
        return True
    return ":" not in wanted and pulled.split(":", 1)[0] == wanted


def _probe_ollama(cfg: _ProviderConfig) -> ProviderInfo:
    """Synchronously check the local Ollama daemon and configured model."""
    root = cfg.base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        response = httpx.get(f"{root}/api/tags", timeout=_OLLAMA_PROBE_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
        models = [
            str(entry.get("name", ""))
            for entry in payload.get("models", [])
            if isinstance(entry, dict)
        ]
    except Exception as exc:  # connection refused, timeout, bad JSON, HTTP error
        logger.debug("Ollama probe failed: %s", exc)
        return ProviderInfo(
            provider="ollama",
            model=cfg.model,
            available=False,
            detail=f"Ollama is not reachable at {root}. {cfg.key_help}",
        )
    if any(_model_matches(cfg.model, name) for name in models):
        return ProviderInfo(
            provider="ollama",
            model=cfg.model,
            available=True,
            detail=f"Ollama is running at {root} and '{cfg.model}' is pulled.",
        )
    return ProviderInfo(
        provider="ollama",
        model=cfg.model,
        available=False,
        detail=(
            f"Ollama is running at {root} but '{cfg.model}' is not pulled. "
            f"Run: ollama pull {cfg.model}"
        ),
    )


def _probe(name: str) -> ProviderInfo:
    """Compute fresh availability status for one provider (uncached)."""
    cfg = _resolve(name)
    if name == "ollama":
        return _probe_ollama(cfg)
    if cfg.api_key:
        return ProviderInfo(
            provider=name,
            model=cfg.model,
            available=True,
            detail=f"{cfg.key_env} is set; using {cfg.base_url}",
        )
    return ProviderInfo(
        provider=name,
        model=cfg.model,
        available=False,
        detail=f"{cfg.key_env} is not set. {cfg.key_help}",
    )


def _status_for(name: str) -> ProviderInfo:
    """Cached (30s, monotonic clock) availability status for one provider."""
    now = time.monotonic()
    cached = _status_cache.get(name)
    if cached is not None and now - cached[0] < _STATUS_TTL_SECONDS:
        return cached[1]
    info = _probe(name)
    _status_cache[name] = (time.monotonic(), info)
    return info


def get_provider_status() -> ProviderInfo:
    """Availability status of the currently configured provider.

    For Ollama this performs a fast (2s timeout) synchronous GET against
    ``{base}/api/tags`` and reports whether the configured model is pulled;
    for cloud providers it reports whether the API key env var is non-empty.
    Results are cached for ~30 seconds.
    """
    return _status_for(_provider_name())


def list_providers() -> List[ProviderInfo]:
    """Availability status of all four supported providers."""
    return [_status_for(name) for name in PROVIDERS]


async def stream_completion(messages: List[dict]) -> AsyncIterator[str]:
    """Stream chat-completion content deltas from the configured provider.

    Uses ``stream=True`` at temperature 0.1 and yields non-empty content
    deltas as they arrive. Any connection, authentication, rate-limit or
    API error is wrapped in :class:`LLMUnavailable` with a human-readable
    message so callers can degrade to deterministic output.
    """
    name = _provider_name()
    cfg = _resolve(name)
    if cfg.key_env is not None and not cfg.api_key:
        raise LLMUnavailable(
            f"LLM provider '{name}' has no API key configured. {cfg.key_help}"
        )
    client = AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key or "ollama",
        timeout=60.0,
        max_retries=0,
    )
    try:
        stream = await client.chat.completions.create(
            model=cfg.model,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            temperature=0.1,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            content = getattr(chunk.choices[0].delta, "content", None)
            if content:
                yield content
    except openai.AuthenticationError as exc:
        logger.warning("LLM auth failure for provider %s: %s", name, exc)
        raise LLMUnavailable(
            f"Authentication with '{name}' failed - check {cfg.key_env or 'the provider setup'}. "
            f"{cfg.key_help}"
        ) from exc
    except (openai.APIConnectionError, openai.APITimeoutError) as exc:
        logger.warning("LLM connection failure for provider %s: %s", name, exc)
        raise LLMUnavailable(
            f"Could not reach '{name}' at {cfg.base_url}. {cfg.key_help}"
        ) from exc
    except openai.RateLimitError as exc:
        logger.warning("LLM rate limit for provider %s: %s", name, exc)
        raise LLMUnavailable(
            f"Provider '{name}' is rate-limiting requests right now - try again shortly."
        ) from exc
    except openai.OpenAIError as exc:
        logger.warning("LLM API error for provider %s: %s", name, exc)
        raise LLMUnavailable(
            f"Provider '{name}' returned an API error ({exc.__class__.__name__}): {exc}"
        ) from exc
    except httpx.HTTPError as exc:  # defensive: transport errors outside the SDK
        logger.warning("LLM transport error for provider %s: %s", name, exc)
        raise LLMUnavailable(
            f"Transport error talking to '{name}' at {cfg.base_url}: {exc}"
        ) from exc
    finally:
        await client.close()
