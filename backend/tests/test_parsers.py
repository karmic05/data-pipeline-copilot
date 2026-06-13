"""Parser contract tests.

``parsers.parse(source)`` on each example must yield a ``ParseResult`` whose
``.ir`` validates as the IR model, reports the expected format, and carries a
non-empty operation list (plus non-empty tables for the SQL/dbt/Flink examples).
"""
from __future__ import annotations

import pytest

from app import parsers
from app.schemas.ir import IR, ParseResult

from .conftest import EXAMPLE_FORMATS, SQL_LIKE_EXAMPLES, read_example


@pytest.mark.parametrize("name,expected_format", sorted(EXAMPLE_FORMATS.items()))
def test_parse_returns_valid_parse_result(name: str, expected_format: str) -> None:
    """Parsing yields a ParseResult with a schema-valid IR of the right format."""
    result = parsers.parse(read_example(name))

    assert isinstance(result, ParseResult)
    assert isinstance(result.ir, IR)
    # ir is a Pydantic model — round-trip it to confirm it still validates.
    IR.model_validate(result.ir.model_dump())

    assert result.ir.format == expected_format
    assert result.source  # the raw source is retained on the result


@pytest.mark.parametrize("name", sorted(EXAMPLE_FORMATS))
def test_parse_has_non_empty_operations(name: str) -> None:
    """Every example surfaces at least one parsed operation in the IR."""
    result = parsers.parse(read_example(name))
    assert len(result.ir.operations) > 0
    for op in result.ir.operations:
        assert op.type  # operation type is always a non-empty string


@pytest.mark.parametrize("name", sorted(SQL_LIKE_EXAMPLES))
def test_sql_like_examples_have_tables(name: str) -> None:
    """SQL / dbt / Flink examples populate non-empty table references."""
    result = parsers.parse(read_example(name))
    assert len(result.ir.tables) > 0
    for table in result.ir.tables:
        assert table.name
        assert table.access_type in {"read", "write", "readwrite"}


def test_parse_with_explicit_format_matches_auto() -> None:
    """Passing the detected format explicitly yields the same IR format."""
    source = read_example("snowflake_orders.sql")
    auto = parsers.parse(source)
    explicit = parsers.parse(source, format="sql")
    assert explicit.ir.format == auto.ir.format == "sql"
