"""Shared pytest fixtures and path setup for the backend test suite.

``pytest.ini`` already sets ``pythonpath = .`` so ``import app`` resolves, but we
also push the backend directory onto ``sys.path`` defensively so the suite runs
regardless of the working directory it is invoked from. Example pipelines are
loaded once from ``backend/examples`` and shared read-only across tests.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import pytest

# --- path setup -------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

EXAMPLES_DIR = BACKEND_DIR / "examples"

#: Maps each example file name to the pipeline format it should detect as.
EXAMPLE_FORMATS: Dict[str, str] = {
    "snowflake_orders.sql": "sql",
    "airflow_etl.py": "airflow",
    "dbt_orders.sql": "dbt",
    "spark_sessions.py": "spark",
    "flink_clicks.sql": "flink",
}

#: Example file names that carry SQL/dbt table references (non-empty ir.tables).
SQL_LIKE_EXAMPLES = {"snowflake_orders.sql", "dbt_orders.sql", "flink_clicks.sql"}


@pytest.fixture(autouse=True, scope="session")
def _force_offline_llm():
    """Force the deterministic offline LLM path for the whole test session.

    Otherwise tests that exercise ``/api/explain`` would hit a live local
    Ollama daemon if one happens to be running, making the suite slow and
    machine-dependent. We point the provider at an unconfigured cloud provider
    (no key → ``available=False``) so ``stream_task`` streams its template
    fallback, and we clear the client's status cache around the session.
    """
    from app.config import settings
    from app.llm import client

    saved = (settings.llm_provider, settings.groq_api_key)
    settings.llm_provider = "groq"
    settings.groq_api_key = ""
    client._status_cache.clear()
    try:
        yield
    finally:
        settings.llm_provider, settings.groq_api_key = saved
        client._status_cache.clear()


def read_example(name: str) -> str:
    """Return the raw text of an example pipeline file."""
    return (EXAMPLES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def examples() -> Dict[str, str]:
    """All example pipeline sources keyed by file name."""
    return {name: read_example(name) for name in EXAMPLE_FORMATS}


@pytest.fixture(scope="session")
def snowflake_src() -> str:
    """The Snowflake SQL example source (used by most analyzer/api tests)."""
    return read_example("snowflake_orders.sql")


@pytest.fixture(scope="session")
def airflow_src() -> str:
    """The Airflow ETL example source."""
    return read_example("airflow_etl.py")
