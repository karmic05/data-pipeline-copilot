"""Sample-pipeline tests over REAL open datasets.

Each example file under ``backend/examples`` is a realistic pipeline built on a
well-known public dataset (TPC-H, NYC TLC trip records, GH Archive, IMDb) that
deliberately embeds classic anti-patterns. For every file we assert that:

- ``detect_format`` fingerprints it as SQL with the expected dialect,
- ``parsers.parse`` yields a schema-valid IR with non-empty tables AND
  operations, and
- ``rules.run_rules`` surfaces at least one issue for the analyzer to show.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import pytest

from app import parsers, rules
from app.schemas.ir import IR, ParseResult

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

#: Each new real-dataset example -> the (format, dialect) it must detect as.
REAL_DATASET_EXAMPLES: dict[str, Tuple[str, Optional[str]]] = {
    "tpch_revenue.sql": ("sql", "snowflake"),
    "nyc_taxi_metrics.sql": ("sql", "snowflake"),
    "github_archive_rollup.sql": ("sql", "bigquery"),
    "imdb_top_titles.sql": ("sql", "snowflake"),
}


def _read(name: str) -> str:
    """Raw text of a real-dataset example pipeline file."""
    return (EXAMPLES_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name,expected", sorted(REAL_DATASET_EXAMPLES.items())
)
def test_detect_format_is_sql_with_expected_dialect(
    name: str, expected: Tuple[str, Optional[str]]
) -> None:
    """Each sample auto-detects as SQL with the intended dialect."""
    fmt, dialect = parsers.detect_format(_read(name))
    assert fmt == "sql"
    assert (fmt, dialect) == expected


@pytest.mark.parametrize("name", sorted(REAL_DATASET_EXAMPLES))
def test_parse_yields_valid_ir_with_tables_and_operations(name: str) -> None:
    """Parsing yields a schema-valid IR with non-empty tables and operations."""
    result = parsers.parse(_read(name))

    assert isinstance(result, ParseResult)
    assert isinstance(result.ir, IR)
    # Round-trip through Pydantic to confirm the IR still validates.
    IR.model_validate(result.ir.model_dump())

    assert result.ir.format == "sql"
    assert len(result.ir.tables) > 0
    for table in result.ir.tables:
        assert table.name
        assert table.access_type in {"read", "write", "readwrite"}

    assert len(result.ir.operations) > 0
    for op in result.ir.operations:
        assert op.type  # operation type is always a non-empty string


@pytest.mark.parametrize("name", sorted(REAL_DATASET_EXAMPLES))
def test_rules_find_at_least_one_issue(name: str) -> None:
    """The deterministic rule engine flags at least one issue per sample."""
    result = parsers.parse(_read(name))
    issues = rules.run_rules(result)
    assert len(issues) >= 1
    for issue in issues:
        assert issue.rule  # every issue names the rule that produced it
