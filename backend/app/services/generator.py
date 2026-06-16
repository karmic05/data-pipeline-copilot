"""Plain-English -> pipeline-code generator.

:func:`generate_pipeline` turns a natural-language request into runnable
pipeline code. When a live LLM provider is configured it asks the model for
production-quality code, strips any stray markdown fences, and (for
``target_format="auto"``) detects the produced format via
:func:`app.parsers.detect_format`.

OFFLINE FALLBACK: with no provider configured it synthesizes a reasonable
starter pipeline deterministically from keywords in the prompt, so the product
generates *something* useful with zero providers. It only raises
:class:`~app.llm.client.LLMUnavailable` when a configured provider errors
mid-call.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from app.llm.client import LLMUnavailable, get_provider_status, stream_completion
from app.llm.gen_prompts import (
    DEFAULT_SQL_DIALECT,
    TARGET_FORMATS,
    build_generate_messages,
)
from app.parsers import detect_format
from app.schemas.agent import GenerateRequest, GenerateResponse

logger = logging.getLogger(__name__)

OFFLINE_NOTE = (
    "Generated offline - configure an LLM provider (LLM_PROVIDER + key) for "
    "higher-quality generation."
)

#: Maps an offline target format to its synthesized-skeleton builder.
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "from", "for", "with", "into",
    "that", "this", "build", "create", "make", "generate", "pipeline", "data",
    "table", "tables", "using", "use", "on", "in", "by", "per", "each", "all",
    "new", "load", "loads", "loading", "etl", "elt", "job", "dag", "model",
    "models", "query", "report", "daily", "hourly", "raw",
})
_NOUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _strip_fences(text: str) -> str:
    """Remove a single wrapping markdown code fence, if present."""
    if not text:
        return ""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    # Looser fallback: drop bare ``` lines anywhere they wrap the block.
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _resolve_dialect(req: GenerateRequest, fmt: str) -> Optional[str]:
    """Resolve the dialect, defaulting to snowflake for SQL-flavored targets."""
    if req.dialect:
        return req.dialect.strip().lower()
    if fmt in ("sql", "dbt"):
        return DEFAULT_SQL_DIALECT
    return None


def _keywords(prompt: str, limit: int = 6) -> List[str]:
    """Extract distinct noun-ish keywords from the prompt for skeleton naming."""
    seen: List[str] = []
    for word in _NOUN_RE.findall(prompt.lower()):
        if word in _STOPWORDS or len(word) < 3 or word in seen:
            continue
        seen.append(word)
        if len(seen) >= limit:
            break
    return seen


# ---------------------------------------------------------------------------
# Offline deterministic skeletons (zero-provider fallback)
# ---------------------------------------------------------------------------


def _offline_sql(keywords: List[str], dialect: str) -> str:
    target = (keywords[0] if keywords else "output") + "_summary"
    source = keywords[1] if len(keywords) > 1 else (keywords[0] if keywords else "source_events")
    dims = keywords[2:5] or ["dimension"]
    select_cols = ",\n    ".join(f"{d} AS {d}" for d in dims)
    group_cols = ", ".join(str(i + 1) for i in range(len(dims)))
    return (
        f"-- {dialect} pipeline (offline skeleton)\n"
        f"-- Source request keywords: {', '.join(keywords) or 'n/a'}\n"
        f"CREATE OR REPLACE TABLE analytics.{target} AS\n"
        f"SELECT\n"
        f"    {select_cols},\n"
        f"    COUNT(*) AS row_count,\n"
        f"    SUM(amount) AS total_amount\n"
        f"FROM raw.{source}\n"
        f"WHERE event_date >= DATEADD('day', -30, CURRENT_DATE)\n"
        f"GROUP BY {group_cols};\n"
    )


def _offline_dbt(keywords: List[str], dialect: str) -> str:
    model = (keywords[0] if keywords else "output") + "_summary"
    source = keywords[1] if len(keywords) > 1 else (keywords[0] if keywords else "events")
    dims = keywords[2:5] or ["dimension"]
    select_cols = ",\n    ".join(dims)
    group_cols = ", ".join(str(i + 1) for i in range(len(dims)))
    return (
        "{{ config(materialized='incremental', unique_key='id') }}\n\n"
        f"-- dbt model {model} (offline skeleton, {dialect} dialect)\n"
        "with source as (\n"
        f"    select * from {{{{ ref('stg_{source}') }}}}\n"
        "),\n\n"
        "final as (\n"
        "    select\n"
        f"    {select_cols},\n"
        "        count(*) as row_count,\n"
        "        sum(amount) as total_amount\n"
        "    from source\n"
        "    {% if is_incremental() %}\n"
        "    where event_date > (select max(event_date) from {{ this }})\n"
        "    {% endif %}\n"
        f"    group by {group_cols}\n"
        ")\n\n"
        "select * from final\n"
    )


def _offline_airflow(keywords: List[str], dialect: str) -> str:
    dag_id = "_".join(keywords[:3]) or "generated_pipeline"
    source = keywords[1] if len(keywords) > 1 else (keywords[0] if keywords else "events")
    return (
        "from datetime import datetime, timedelta\n\n"
        "from airflow import DAG\n"
        "from airflow.operators.python import PythonOperator\n\n"
        f"# Airflow DAG {dag_id} (offline skeleton)\n"
        "default_args = {\n"
        '    "retries": 2,\n'
        '    "retry_delay": timedelta(minutes=5),\n'
        "}\n\n"
        "with DAG(\n"
        f'    dag_id="{dag_id}",\n'
        '    schedule="@daily",\n'
        "    start_date=datetime(2024, 1, 1),\n"
        "    catchup=False,\n"
        "    default_args=default_args,\n"
        ") as dag:\n\n"
        "    def extract(**context):\n"
        f'        """Extract {source} records for the run date."""\n'
        "        ...\n\n"
        "    def transform(**context):\n"
        '        """Clean and aggregate the extracted records."""\n'
        "        ...\n\n"
        "    def load(**context):\n"
        '        """Write the result to the analytics warehouse."""\n'
        "        ...\n\n"
        '    extract_task = PythonOperator(task_id="extract", python_callable=extract)\n'
        '    transform_task = PythonOperator(task_id="transform", python_callable=transform)\n'
        '    load_task = PythonOperator(task_id="load", python_callable=load)\n\n'
        "    extract_task >> transform_task >> load_task\n"
    )


def _offline_spark(keywords: List[str], dialect: str) -> str:
    source = keywords[1] if len(keywords) > 1 else (keywords[0] if keywords else "events")
    target = (keywords[0] if keywords else "output") + "_summary"
    dims = keywords[2:5] or ["dimension"]
    group = ", ".join(f'"{d}"' for d in dims)
    return (
        "from pyspark.sql import SparkSession, functions as F\n\n"
        f"# PySpark job for {target} (offline skeleton)\n"
        'spark = SparkSession.builder.appName("' + target + '").getOrCreate()\n\n'
        f'source_df = spark.read.parquet("s3://lake/raw/{source}/")\n\n'
        "result_df = (\n"
        "    source_df\n"
        f"    .groupBy({group})\n"
        '    .agg(F.count("*").alias("row_count"), F.sum("amount").alias("total_amount"))\n'
        ")\n\n"
        "(\n"
        "    result_df.write\n"
        '    .mode("overwrite")\n'
        f'    .partitionBy({group})\n'
        f'    .parquet("s3://lake/analytics/{target}/")\n'
        ")\n\n"
        "spark.stop()\n"
    )


def _offline_flink(keywords: List[str], dialect: str) -> str:
    source = keywords[1] if len(keywords) > 1 else (keywords[0] if keywords else "events")
    target = (keywords[0] if keywords else "output") + "_summary"
    return (
        f"-- Flink SQL streaming pipeline for {target} (offline skeleton)\n"
        f"CREATE TABLE {source}_src (\n"
        "    id BIGINT,\n"
        "    amount DECIMAL(12, 2),\n"
        "    event_time TIMESTAMP(3),\n"
        "    WATERMARK FOR event_time AS event_time - INTERVAL '5' SECOND\n"
        ") WITH (\n"
        "    'connector' = 'kafka',\n"
        f"    'topic' = '{source}',\n"
        "    'properties.bootstrap.servers' = 'localhost:9092',\n"
        "    'format' = 'json'\n"
        ");\n\n"
        f"CREATE TABLE {target}_sink (\n"
        "    window_start TIMESTAMP(3),\n"
        "    total_amount DECIMAL(12, 2),\n"
        "    row_count BIGINT\n"
        ") WITH (\n"
        "    'connector' = 'jdbc',\n"
        f"    'table-name' = '{target}'\n"
        ");\n\n"
        f"INSERT INTO {target}_sink\n"
        "SELECT\n"
        "    window_start,\n"
        "    SUM(amount) AS total_amount,\n"
        "    COUNT(*) AS row_count\n"
        f"FROM TABLE(TUMBLE(TABLE {source}_src, DESCRIPTOR(event_time), INTERVAL '1' HOUR))\n"
        "GROUP BY window_start;\n"
    )


_OFFLINE_BUILDERS = {
    "sql": _offline_sql,
    "dbt": _offline_dbt,
    "airflow": _offline_airflow,
    "spark": _offline_spark,
    "flink": _offline_flink,
}


def _offline_pipeline(req: GenerateRequest) -> GenerateResponse:
    """Synthesize a deterministic starter pipeline from the prompt keywords."""
    fmt = (req.target_format or "auto").strip().lower()
    if fmt not in TARGET_FORMATS:
        fmt = "auto"
    if fmt == "auto":
        fmt = "sql"  # most universally runnable starting point
    dialect = _resolve_dialect(req, fmt)
    keywords = _keywords(req.prompt)
    builder = _OFFLINE_BUILDERS.get(fmt, _offline_sql)
    code = builder(keywords, dialect or DEFAULT_SQL_DIALECT)
    return GenerateResponse(
        code=code,
        format=fmt,
        dialect=dialect,
        notes=[OFFLINE_NOTE],
    )


async def _complete(messages: List[dict]) -> str:
    """Accumulate a streamed completion into a single string."""
    parts: List[str] = []
    async for tok in stream_completion(messages):
        parts.append(tok)
    return "".join(parts)


def _detect_generated_format(code: str, requested: str) -> str:
    """Detect the produced format for ``auto`` requests via the parser fingerprint."""
    if requested and requested != "auto":
        return requested
    try:
        fmt, _ = detect_format(code)
        return fmt
    except Exception:
        # Detection failed (e.g. an unusual snippet); SQL is the safe default.
        logger.debug("Format auto-detection failed for generated code; defaulting to sql")
        return "sql"


async def generate_pipeline(req: GenerateRequest) -> GenerateResponse:
    """Generate runnable pipeline code from a plain-English request.

    With a live provider configured, the LLM produces production-quality code
    honoring ``target_format`` and ``dialect``; markdown fences are stripped and
    the format is detected via the parser when ``target_format="auto"``.

    With NO provider configured, a deterministic starter skeleton is synthesized
    from the prompt keywords and tagged with an offline note. This function only
    raises :class:`LLMUnavailable` when a *configured* provider errors mid-call.
    """
    requested_fmt = (req.target_format or "auto").strip().lower()
    if requested_fmt not in TARGET_FORMATS:
        requested_fmt = "auto"

    try:
        provider_available = get_provider_status().available
    except Exception:  # probe failure -> treat as offline, never raise here
        logger.debug("Provider status probe failed; using offline generation")
        provider_available = False

    if not provider_available:
        return _offline_pipeline(req)

    # A provider is configured: any failure here is a real error worth raising.
    messages = build_generate_messages(req.prompt, requested_fmt, req.dialect)
    raw = await _complete(messages)  # may raise LLMUnavailable
    code = _strip_fences(raw)
    if not code:
        # Provider responded but produced nothing usable; fall back gracefully
        # rather than handing back an empty file.
        logger.warning("Generator produced empty output; using offline skeleton")
        fallback = _offline_pipeline(req)
        fallback.notes.append("Live provider returned empty output; used offline skeleton.")
        return fallback

    fmt = _detect_generated_format(code, requested_fmt)
    dialect = _resolve_dialect(req, fmt)
    notes: List[str] = []
    if requested_fmt == "auto":
        notes.append(f"Auto-detected format: {fmt}.")
    return GenerateResponse(code=code, format=fmt, dialect=dialect, notes=notes)
