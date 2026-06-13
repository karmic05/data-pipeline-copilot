"""Format auto-detection contract tests.

``parsers.detect_format`` must fingerprint each example's raw content to the
expected ``PipelineFormat`` with no manual format hint.
"""
from __future__ import annotations

import pytest

from app import parsers
from app.schemas.ir import ParseError

from .conftest import EXAMPLE_FORMATS, read_example


@pytest.mark.parametrize("name,expected_format", sorted(EXAMPLE_FORMATS.items()))
def test_detect_format(name: str, expected_format: str) -> None:
    """Each example detects as its intended pipeline format."""
    source = read_example(name)
    detected, _dialect = parsers.detect_format(source)
    assert detected == expected_format


def test_snowflake_detects_dialect() -> None:
    """The Snowflake example carries a non-empty SQL dialect hint."""
    _fmt, dialect = parsers.detect_format(read_example("snowflake_orders.sql"))
    assert dialect == "snowflake"


def test_unrecognized_input_raises_parse_error() -> None:
    """Garbage that matches no fingerprint raises ParseError, not a crash."""
    with pytest.raises(ParseError):
        parsers.detect_format("!!!not a pipeline!!!")
