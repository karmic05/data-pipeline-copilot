"""Format auto-detection and parse dispatch.

``detect_format`` fingerprints raw source content - the user never selects a
pipeline type manually. ``parse`` dispatches to the per-format parser modules,
each of which returns a :class:`app.schemas.ir.ParseResult`.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from app.schemas.ir import ParseError, ParseResult, PipelineFormat

_SQL_HINT = re.compile(
    r"\b(select|insert\s+into|create\s+(or\s+replace\s+)?(table|view|materialized)|"
    r"merge\s+into|delete\s+from|update\s+\w+\s+set|with\s+\w+\s+as)\b",
    re.IGNORECASE,
)


def detect_format(source: str) -> Tuple[PipelineFormat, Optional[str]]:
    """Return ``(format, dialect)`` from a content fingerprint.

    Detection order matters: Python frameworks first (their files may embed
    SQL strings), then dbt (Jinja markers), then Flink (streaming DDL markers),
    then plain SQL as the fallback.
    """
    text = source.strip()
    lower = text.lower()

    # --- Python-based frameworks -------------------------------------------
    if re.search(r"^\s*(from|import)\s+airflow", text, re.MULTILINE) or "airflow.operators" in lower:
        return "airflow", None
    if re.search(r"^\s*(from|import)\s+prefect", text, re.MULTILINE) or re.search(
        r"^\s*(from|import)\s+dagster", text, re.MULTILINE
    ):
        return "prefect", None
    if "pyspark" in lower or "sparksession" in text or "spark.read" in lower:
        return "spark", None
    if "streamsbuilder" in lower or "kafkastreams" in lower or re.search(
        r"^\s*(from|import)\s+(kafka|confluent_kafka|faust)", text, re.MULTILINE
    ):
        return "kafka", None

    # --- Great Expectations (JSON/YAML suite) ------------------------------
    if "expectation_suite_name" in lower or "expectation_type" in lower:
        return "great_expectations", None

    # --- dbt (Jinja markers or model schema.yml) ----------------------------
    if re.search(r"\{\{\s*(ref|source|config)\s*\(", text) or re.search(
        r"^\s*models\s*:", text, re.MULTILINE
    ):
        return "dbt", _detect_dialect(text)

    # --- Flink streaming SQL -------------------------------------------------
    if re.search(r"\bwatermark\s+for\b", lower) or re.search(
        r"\b(tumble|hop|session|cumulate)\s*\(", lower
    ) or ("'connector'" in lower and _SQL_HINT.search(text)):
        return "flink", "flink"

    # --- Plain SQL -----------------------------------------------------------
    if _SQL_HINT.search(text):
        return "sql", _detect_dialect(text)

    raise ParseError(
        "Could not detect pipeline format. Supported inputs: SQL, Airflow DAGs, "
        "dbt models, PySpark jobs, Prefect/Dagster flows, Flink SQL, Kafka "
        "Streams topologies, Great Expectations suites."
    )


def _detect_dialect(text: str) -> str:
    """Heuristic SQL dialect detection for warehouse-aware rules and costs."""
    lower = text.lower()
    if re.search(r"\b(qualify|ilike)\b", lower) or "flatten(" in lower or "iff(" in lower:
        return "snowflake"
    if "`" in text and re.search(r"\b(struct|unnest|safe_cast|_partitiondate|_table_suffix)\b", lower):
        return "bigquery"
    if re.search(r"\b(distkey|sortkey|diststyle)\b", lower):
        return "redshift"
    if re.search(r"\b(unnest|cross\s+join\s+unnest|approx_distinct)\b", lower) and "`" not in text:
        return "trino"
    if "`" in text:
        return "bigquery"
    if "::" in text:
        return "postgres"
    return "snowflake"


def parse(
    source: str,
    format: str = "auto",
    dialect: Optional[str] = None,
) -> ParseResult:
    """Parse raw pipeline source into a validated ParseResult.

    Raises :class:`ParseError` on undetectable or unparseable input.
    """
    if not source or not source.strip():
        raise ParseError("Empty input - paste pipeline code to analyze.")

    fmt: PipelineFormat
    detected_dialect: Optional[str] = None
    if format == "auto" or not format:
        fmt, detected_dialect = detect_format(source)
    else:
        fmt = format  # type: ignore[assignment]
    dialect = dialect or detected_dialect

    if fmt == "sql":
        from app.parsers.sql_parser import parse_sql

        return parse_sql(source, dialect)
    if fmt == "flink":
        from app.parsers.sql_parser import parse_flink

        return parse_flink(source)
    if fmt == "airflow":
        from app.parsers.airflow_parser import parse_airflow

        return parse_airflow(source)
    if fmt == "prefect":
        from app.parsers.airflow_parser import parse_prefect

        return parse_prefect(source)
    if fmt == "spark":
        from app.parsers.spark_parser import parse_spark

        return parse_spark(source)
    if fmt == "kafka":
        from app.parsers.streaming_parser import parse_kafka

        return parse_kafka(source)
    if fmt == "dbt":
        from app.parsers.dbt_parser import parse_dbt

        return parse_dbt(source, dialect)
    if fmt == "great_expectations":
        from app.parsers.ge_parser import parse_great_expectations

        return parse_great_expectations(source)

    raise ParseError(f"Unsupported format: {fmt}")
