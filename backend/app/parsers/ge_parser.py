"""Great Expectations suite parser.

Accepts an expectation suite serialized as JSON (preferred) or YAML and turns
it into the unified IR: one ``EXPECTATION`` operation per expectation with
``details = {"expectation_type", "column", "kwargs"}``, a best-effort
:class:`~app.schemas.ir.TableRef` derived from the suite / batch asset name,
and the full parsed suite preserved in ``extras["suite"]``.

Both the classic suite shape (``expectation_suite_name`` +
``expectations[].expectation_type``) and the GE 1.x shape (``name`` +
``expectations[].type``) are supported.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import yaml

from app.schemas.ir import IR, Location, Operation, ParseError, ParseResult, TableRef

logger = logging.getLogger(__name__)

_SUITE_NAME_SUFFIXES = {
    "warning",
    "failure",
    "suite",
    "basic",
    "default",
    "critical",
    "expectations",
}


def _dig(data: Any, *keys: str) -> Any:
    """Safely walk nested dicts, returning ``None`` on any miss."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _looks_like_expectation(entry: Any) -> bool:
    """True when an entry has the shape of a serialized expectation."""
    return isinstance(entry, dict) and (
        "expectation_type" in entry or "type" in entry
    )


def _load_suite(source: str) -> Dict[str, Any]:
    """Decode the raw input as JSON, falling back to YAML.

    Raises:
        ParseError: when the input is neither valid JSON nor valid YAML, or
            does not decode to a mapping (a bare list of expectations is
            tolerated and wrapped).
    """
    loaded: Any
    try:
        loaded = json.loads(source)
    except ValueError:
        try:
            loaded = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ParseError(
                f"Great Expectations input is neither valid JSON nor valid YAML: {exc}"
            ) from exc
    if isinstance(loaded, list):
        if loaded and all(_looks_like_expectation(entry) for entry in loaded):
            return {"expectations": loaded}
        raise ParseError(
            "Great Expectations input is a list but not a list of expectations."
        )
    if not isinstance(loaded, dict):
        raise ParseError(
            "Great Expectations input did not decode to a JSON/YAML mapping."
        )
    return loaded


def _locate(lines: List[str], needle: str, cursor: Dict[str, int]) -> int:
    """Best-effort 1-based line number of ``needle`` in the raw source.

    ``cursor`` tracks the scan position per needle so repeated expectation
    types map to successive occurrences. Returns 0 when not found.
    """
    start = cursor.get(needle, 0)
    for index in range(start, len(lines)):
        if needle in lines[index]:
            cursor[needle] = index + 1
            return index + 1
    return 0


def _derive_table(suite: Dict[str, Any], columns: List[str]) -> Optional[TableRef]:
    """Derive a TableRef from the batch asset name or the suite name.

    Checks the common locations for ``data_asset_name`` first, then falls back
    to the suite name, stripping conventional suite-name suffixes (e.g.
    ``analytics.orders.warning`` -> schema ``analytics``, table ``orders``).
    """
    asset = (
        _dig(suite, "meta", "batch_kwargs", "data_asset_name")
        or _dig(suite, "meta", "batch_spec", "data_asset_name")
        or _dig(suite, "meta", "batch_request", "data_asset_name")
        or _dig(suite, "batch_kwargs", "data_asset_name")
        or suite.get("data_asset_name")
    )
    candidate: Optional[str] = asset if isinstance(asset, str) and asset.strip() else None
    if candidate is None:
        suite_name = suite.get("expectation_suite_name") or suite.get("name")
        if isinstance(suite_name, str) and suite_name.strip():
            candidate = suite_name
    if candidate is None:
        return None

    parts = [part for part in candidate.strip().split(".") if part]
    if len(parts) > 1 and parts[-1].lower() in _SUITE_NAME_SUFFIXES:
        parts = parts[:-1]
    if not parts:
        return None
    return TableRef(
        name=parts[-1],
        schema_name=parts[-2] if len(parts) >= 2 else None,
        database=parts[-3] if len(parts) >= 3 else None,
        columns=list(columns),
        access_type="read",
    )


def _suite_description(suite: Dict[str, Any]) -> Optional[str]:
    """Pull a human description from ``meta.notes`` when present."""
    notes = _dig(suite, "meta", "notes")
    if isinstance(notes, str) and notes.strip():
        return notes.strip()
    if isinstance(notes, dict):
        content = notes.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            joined = " ".join(str(item) for item in content).strip()
            return joined or None
    return None


def parse_great_expectations(source: str) -> ParseResult:
    """Parse a Great Expectations suite (JSON or YAML) into a ParseResult.

    Raises:
        ParseError: when the input is empty, undecodable, or does not look
            like an expectation suite.
    """
    if not source or not source.strip():
        raise ParseError("Empty input — paste a Great Expectations suite to analyze.")

    suite = _load_suite(source)
    warnings: List[str] = []

    suite_name = suite.get("expectation_suite_name") or suite.get("name")
    expectations = suite.get("expectations")
    if expectations is None:
        if not (isinstance(suite_name, str) and suite_name.strip()):
            raise ParseError(
                "Input does not look like a Great Expectations suite "
                "(no 'expectations' list and no suite name)."
            )
        expectations = []
        warnings.append("Suite has no 'expectations' list.")
    if not isinstance(expectations, list):
        raise ParseError("Great Expectations 'expectations' must be a list.")

    operations: List[Operation] = []
    columns: List[str] = []
    source_lines = source.splitlines()
    cursor: Dict[str, int] = {}
    for index, expectation in enumerate(expectations):
        if not isinstance(expectation, dict):
            warnings.append(f"Skipped expectation #{index + 1}: not a mapping.")
            continue
        expectation_type = expectation.get("expectation_type") or expectation.get("type")
        if not isinstance(expectation_type, str) or not expectation_type.strip():
            warnings.append(
                f"Skipped expectation #{index + 1}: missing expectation_type."
            )
            continue
        expectation_type = expectation_type.strip()
        raw_kwargs = expectation.get("kwargs")
        kwargs: Dict[str, Any] = raw_kwargs if isinstance(raw_kwargs, dict) else {}
        column = kwargs.get("column")
        if isinstance(column, str) and column and column not in columns:
            columns.append(column)
        line = _locate(source_lines, expectation_type, cursor)
        operations.append(
            Operation(
                type="EXPECTATION",
                location=Location(line=line) if line else Location(),
                details={
                    "expectation_type": expectation_type,
                    "column": column,
                    "kwargs": kwargs,
                },
            )
        )

    ir = IR(format="great_expectations", operations=operations)
    table = _derive_table(suite, columns)
    if table is not None:
        ir.tables.append(table)
    if isinstance(suite_name, str) and suite_name.strip():
        ir.metadata.name = suite_name.strip()
    description = _suite_description(suite)
    if description:
        ir.metadata.description = description

    logger.debug(
        "Parsed Great Expectations suite '%s': %d expectations, %d columns",
        ir.metadata.name or "<unnamed>",
        len(operations),
        len(columns),
    )
    return ParseResult(
        ir=ir,
        source=source,
        ast=suite,
        extras={"suite": suite},
        warnings=warnings,
    )
