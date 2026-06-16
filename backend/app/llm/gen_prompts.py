"""Prompt construction for the plain-English -> pipeline-code generator.

``build_generate_messages`` pairs a senior-data-engineer system prompt with a
per-request instruction derived from :class:`~app.schemas.agent.GenerateRequest`
(target framework + SQL dialect). The model is asked to emit ONLY runnable
pipeline code - no prose, no markdown fences - which
:func:`app.services.generator.generate_pipeline` then strips and format-detects.
"""
from __future__ import annotations

from typing import Dict, List, Optional

#: Default SQL dialect when the caller asks for SQL/dbt without specifying one.
DEFAULT_SQL_DIALECT = "snowflake"

#: Accepted ``target_format`` values for the generator.
TARGET_FORMATS = ("auto", "sql", "dbt", "airflow", "spark", "flink")

GEN_SYSTEM_PROMPT: str = (
    "You are a senior data engineer. Generate production-quality, runnable "
    "pipeline code for the requested framework from the user's plain-English "
    "description. Use realistic, descriptive table and column names and "
    "sensible schemas. Follow the idioms and best practices of the target "
    "framework (incremental models, idempotent writes, partitioning, retries, "
    "and explicit dependencies where appropriate).\n"
    "\n"
    "Output ONLY the pipeline code for the requested framework. No prose, no "
    "explanation, no surrounding markdown code fences - just the raw code, "
    "ready to save to a file and run."
)

#: Per-format framing appended to the instruction so the model targets the
#: right artifact shape.
_FORMAT_GUIDANCE: Dict[str, str] = {
    "sql": (
        "Write a single SQL script (CREATE TABLE / INSERT / MERGE / SELECT as "
        "appropriate) in {dialect} dialect."
    ),
    "dbt": (
        "Write a dbt model: a SELECT-based model file using {{{{ ref() }}}} / "
        "{{{{ source() }}}} and a {{{{ config(...) }}}} block, in {dialect} "
        "SQL dialect."
    ),
    "airflow": (
        "Write a complete Apache Airflow DAG in Python using modern operators "
        "(e.g. @dag/@task or classic operators), with a schedule, retries and "
        "clear task dependencies."
    ),
    "spark": (
        "Write a PySpark job using a SparkSession, DataFrame reads/transforms/"
        "writes, with explicit schemas and partitioned output."
    ),
    "flink": (
        "Write Flink SQL: CREATE TABLE source/sink DDL with connectors and "
        "WATERMARK, plus the streaming INSERT INTO ... SELECT statement."
    ),
}


def _normalize_dialect(target_format: str, dialect: Optional[str]) -> Optional[str]:
    """Resolve the SQL dialect, defaulting to snowflake for SQL-flavored targets."""
    if dialect:
        return dialect.strip().lower()
    if target_format in ("sql", "dbt"):
        return DEFAULT_SQL_DIALECT
    return None


def build_generate_messages(
    prompt: str,
    target_format: str = "auto",
    dialect: Optional[str] = None,
) -> List[dict]:
    """Build the ``[system, user]`` chat messages for code generation.

    ``target_format`` selects the framework (``auto`` lets the model pick the
    most natural fit). ``dialect`` defaults to snowflake for SQL/dbt targets.
    """
    fmt = (target_format or "auto").strip().lower()
    if fmt not in TARGET_FORMATS:
        fmt = "auto"
    resolved_dialect = _normalize_dialect(fmt, dialect)

    if fmt == "auto":
        framing = (
            "Choose the single most appropriate framework for this task "
            "(plain SQL, dbt, Airflow, PySpark, or Flink SQL) and write it. "
            "Default to "
            f"{resolved_dialect or DEFAULT_SQL_DIALECT} dialect for any SQL."
        )
    else:
        framing = _FORMAT_GUIDANCE[fmt].format(
            dialect=resolved_dialect or DEFAULT_SQL_DIALECT
        )

    user_content = (
        f"Task: {prompt.strip()}\n\n"
        f"{framing}\n\n"
        "Remember: output ONLY the code, with no prose and no markdown fences."
    )
    return [
        {"role": "system", "content": GEN_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
