"""Observability engine — auto-generated data tests and coverage gaps.

From the IR's tables (write targets and read sources) and their known columns,
:func:`generate_tests` produces:

- ``dbt_yaml`` — a valid ``version: 2`` dbt ``models:`` schema block with
  per-column ``unique`` / ``not_null`` / ``accepted_values`` /
  ``relationships`` tests,
- ``great_expectations_json`` — an equivalent Great Expectations suite,
- ``tests`` — a flat :class:`~app.schemas.report.GeneratedTest` list matching
  both artifacts,
- ``coverage_gaps`` — human-readable monitoring gaps derived from
  observability-category issues plus IR signals (missing SLA, untested
  sources, no freshness checks, no row-count anomaly monitoring).

Both string artifacts are round-trip validated (``yaml.safe_load`` /
``json.loads``) before being returned, so consumers can always parse them.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Set, Tuple

import yaml

from app.schemas.ir import IR, ParseResult, TableRef
from app.schemas.report import GeneratedTest, GeneratedTests, Issue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
#: Maximum number of tables rendered into the generated artifacts.
MAX_TABLES: int = 8
#: Maximum number of columns considered per table.
MAX_COLUMNS_PER_TABLE: int = 25
#: Maximum number of coverage-gap strings returned.
MAX_COVERAGE_GAPS: int = 12
#: Maximum number of output tables called out for row-count monitoring.
MAX_ROWCOUNT_GAP_TABLES: int = 3

#: Placeholder value set emitted for ``accepted_values`` stubs — the engine
#: cannot know the valid enum domain, only that one should be enforced.
ACCEPTED_VALUES_STUB: List[str] = ["<replace with the column's valid values>"]

# Column-name classifiers (regex over names is fine per project conventions —
# this is fingerprinting, not SQL parsing).
_DATE_SUFFIXES: Tuple[str, ...] = ("_at", "_ts", "_time", "_date", "_day")
_DATE_TOKENS: Tuple[str, ...] = ("date", "timestamp")
_AMOUNT_TOKENS: Tuple[str, ...] = (
    "amount",
    "price",
    "cost",
    "total",
    "revenue",
    "balance",
    "quantity",
    "qty",
    "fee",
    "spend",
)
_ENUM_NAMES: Set[str] = {"status", "type", "state"}
_ENUM_SUFFIXES: Tuple[str, ...] = ("_status", "_type", "_state")

_EMPTY_DBT_YAML: str = yaml.safe_dump(
    {"version": 2, "models": []}, sort_keys=False, default_flow_style=False
)
_EMPTY_GE_SUITE: str = json.dumps(
    {
        "expectation_suite_name": "pipeline_suite",
        "expectations": [],
        "meta": {"generated_by": "data-pipeline-copilot"},
    },
    indent=2,
)


# ---------------------------------------------------------------------------
# Column-name classifiers
# ---------------------------------------------------------------------------
def _is_date_ish(name: str) -> bool:
    """True when a column name looks like a date/timestamp column."""
    lc = name.lower()
    return lc == "ts" or lc.endswith(_DATE_SUFFIXES) or any(t in lc for t in _DATE_TOKENS)


def _is_amount_ish(name: str) -> bool:
    """True when a column name looks like a monetary/quantity measure."""
    lc = name.lower()
    return any(t in lc for t in _AMOUNT_TOKENS)


def _is_enum_ish(name: str) -> bool:
    """True when a column name looks like a small categorical/enum column."""
    lc = name.lower()
    return lc in _ENUM_NAMES or lc.endswith(_ENUM_SUFFIXES)


def _sanitize_identifier(name: str) -> str:
    """Reduce an arbitrary table/model name to a safe snake_case identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()
    return cleaned or "pipeline"


def _ref_name(table_name: str) -> str:
    """The dbt ``ref()`` target for a (possibly schema-qualified) table name."""
    return table_name.split(".")[-1].strip() or table_name


# ---------------------------------------------------------------------------
# IR extraction helpers
# ---------------------------------------------------------------------------
def _unique_keys_from_extras(pr: ParseResult) -> Set[str]:
    """Configured ``unique_key`` column(s) from parser extras, lowercased."""
    keys: Set[str] = set()
    extras = pr.extras if isinstance(pr.extras, dict) else {}
    raw = extras.get("unique_key")
    if isinstance(raw, str) and raw.strip():
        keys.add(raw.strip().lower())
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                keys.add(item.strip().lower())
    return keys


def _ordered_tables(ir: IR) -> List[TableRef]:
    """Write targets first (IR order), then read sources, deduped by name."""
    ordered: List[TableRef] = []
    seen: Set[str] = set()
    writers = [t for t in ir.tables if t.access_type in ("write", "readwrite")]
    readers = [t for t in ir.tables if t.access_type == "read"]
    for table in writers + readers:
        name = (table.name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        ordered.append(table)
    return ordered[:MAX_TABLES]


def _columns_for(table: TableRef, ir: IR) -> List[str]:
    """Known columns for a table: declared columns plus lineage outputs."""
    columns: List[str] = []
    seen: Set[str] = set()
    for col in table.columns:
        if not isinstance(col, str):
            continue
        name = col.strip()
        if not name or name == "*" or name.endswith(".*") or name.lower() in seen:
            continue
        seen.add(name.lower())
        columns.append(name)
    if table.access_type in ("write", "readwrite"):
        for cl in ir.column_lineage:
            if cl.output_table != table.name:
                continue
            name = (cl.output_column or "").strip()
            if not name or name == "*" or name.lower() in seen:
                continue
            seen.add(name.lower())
            columns.append(name)
    return columns[:MAX_COLUMNS_PER_TABLE]


def _relationship_targets(ir: IR) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """``(output_table, *_id column) -> (referenced table, referenced column)``.

    A relationships test is only generated when column lineage shows a
    ``*_id`` output column sourced from a *different* table.
    """
    targets: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for cl in ir.column_lineage:
        out_col = (cl.output_column or "").strip()
        src_table = (cl.source_table or "").strip()
        src_col = (cl.source_column or "").strip()
        out_table = (cl.output_table or "").strip()
        if not (out_col and src_table and src_col and out_table):
            continue
        if not out_col.lower().endswith("_id"):
            continue
        if _ref_name(src_table).lower() == _ref_name(out_table).lower():
            continue
        key = (out_table.lower(), out_col.lower())
        targets.setdefault(key, (src_table, src_col))
    return targets


def _key_columns(columns: List[str], unique_keys: Set[str]) -> Set[str]:
    """Columns that deserve ``unique + not_null`` (lowercased names).

    Keys are the literal ``id`` column and any configured ``unique_key``;
    when neither exists, the first ``*_id`` column stands in as the grain.
    """
    keys: Set[str] = set()
    lowered = [c.lower() for c in columns]
    if "id" in lowered:
        keys.add("id")
    keys.update(k for k in unique_keys if k in lowered)
    if not keys:
        for lc in lowered:
            if lc.endswith("_id"):
                keys.add(lc)
                break
    return keys


# ---------------------------------------------------------------------------
# Artifact builders
# ---------------------------------------------------------------------------
def _build_artifacts(
    pr: ParseResult,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[GeneratedTest]]:
    """Build the dbt schema doc, GE suite doc and flat test list from the IR."""
    ir = pr.ir
    unique_keys = _unique_keys_from_extras(pr)
    rel_targets = _relationship_targets(ir)
    tables = _ordered_tables(ir)

    models: List[Dict[str, Any]] = []
    expectations: List[Dict[str, Any]] = []
    tests: List[GeneratedTest] = []

    for table in tables:
        columns = _columns_for(table, ir)
        if not columns:
            continue
        keys = _key_columns(columns, unique_keys)
        column_entries: List[Dict[str, Any]] = []

        for column in columns:
            lc = column.lower()
            target = f"{table.name}.{column}"
            dbt_tests: List[Any] = []

            if lc in keys:
                dbt_tests.extend(["unique", "not_null"])
                expectations.append(_expectation("expect_column_values_to_be_unique", table.name, column))
                expectations.append(_expectation("expect_column_values_to_not_be_null", table.name, column))
                tests.append(GeneratedTest(framework="dbt", target=target, name="unique",
                                           description=f"Key column '{column}' must be unique per row."))
                tests.append(GeneratedTest(framework="dbt", target=target, name="not_null",
                                           description=f"Key column '{column}' must never be NULL."))
                tests.append(GeneratedTest(framework="great_expectations", target=target,
                                           name="expect_column_values_to_be_unique",
                                           description=f"Key column '{column}' must be unique per row."))
                tests.append(GeneratedTest(framework="great_expectations", target=target,
                                           name="expect_column_values_to_not_be_null",
                                           description=f"Key column '{column}' must never be NULL."))
            elif _is_date_ish(column) or _is_amount_ish(column):
                dbt_tests.append("not_null")
                expectations.append(_expectation("expect_column_values_to_not_be_null", table.name, column))
                kind = "date" if _is_date_ish(column) else "amount"
                tests.append(GeneratedTest(framework="dbt", target=target, name="not_null",
                                           description=f"{kind.title()} column '{column}' must never be NULL."))
                tests.append(GeneratedTest(framework="great_expectations", target=target,
                                           name="expect_column_values_to_not_be_null",
                                           description=f"{kind.title()} column '{column}' must never be NULL."))

            if _is_amount_ish(column):
                expectations.append(
                    _expectation(
                        "expect_column_values_to_be_between",
                        table.name,
                        column,
                        min_value=0,
                    )
                )
                tests.append(GeneratedTest(framework="great_expectations", target=target,
                                           name="expect_column_values_to_be_between",
                                           description=f"Amount column '{column}' must be non-negative."))

            if _is_enum_ish(column):
                dbt_tests.append({"accepted_values": {"values": list(ACCEPTED_VALUES_STUB)}})
                expectations.append(
                    _expectation(
                        "expect_column_values_to_be_in_set",
                        table.name,
                        column,
                        value_set=list(ACCEPTED_VALUES_STUB),
                    )
                )
                tests.append(GeneratedTest(framework="dbt", target=target, name="accepted_values",
                                           description=f"Enum column '{column}' should be restricted to its valid value set (stub — fill in)."))
                tests.append(GeneratedTest(framework="great_expectations", target=target,
                                           name="expect_column_values_to_be_in_set",
                                           description=f"Enum column '{column}' should be restricted to its valid value set (stub — fill in)."))

            rel = rel_targets.get((table.name.lower(), lc))
            if rel is not None:
                ref_table, ref_field = rel
                dbt_tests.append(
                    {
                        "relationships": {
                            "to": f"ref('{_ref_name(ref_table)}')",
                            "field": ref_field,
                        }
                    }
                )
                tests.append(GeneratedTest(framework="dbt", target=target, name="relationships",
                                           description=f"'{column}' must reference an existing {_ref_name(ref_table)}.{ref_field}."))

            if dbt_tests:
                column_entries.append({"name": column, "tests": dbt_tests})

        if column_entries:
            models.append(
                {
                    "name": _ref_name(table.name),
                    "description": "Tests auto-generated by Data Pipeline Copilot.",
                    "columns": column_entries,
                }
            )

    suite_basis = (
        ir.metadata.name
        or next((t.name for t in tables if t.access_type in ("write", "readwrite")), None)
        or (tables[0].name if tables else "pipeline")
    )
    dbt_doc: Dict[str, Any] = {"version": 2, "models": models}
    ge_doc: Dict[str, Any] = {
        "expectation_suite_name": f"{_sanitize_identifier(suite_basis)}_suite",
        "expectations": expectations,
        "meta": {
            "generated_by": "data-pipeline-copilot",
            "source_format": ir.format,
        },
    }
    return dbt_doc, ge_doc, tests


def _expectation(
    expectation_type: str, table: str, column: str, **kwargs: Any
) -> Dict[str, Any]:
    """Build one GE expectation dict; the table is recorded in ``meta``."""
    return {
        "expectation_type": expectation_type,
        "kwargs": {"column": column, **kwargs},
        "meta": {"table": table},
    }


# ---------------------------------------------------------------------------
# Coverage gaps
# ---------------------------------------------------------------------------
def _has_existing_tests(pr: ParseResult) -> bool:
    """Whether the pipeline already carries its own data tests."""
    if pr.ir.format == "great_expectations":
        return True
    extras = pr.extras if isinstance(pr.extras, dict) else {}
    existing = extras.get("tests")
    return bool(existing) if isinstance(existing, (list, tuple, dict)) else False


def _has_freshness_checks(pr: ParseResult) -> bool:
    """Whether any dbt source declares a freshness block."""
    extras = pr.extras if isinstance(pr.extras, dict) else {}
    schema_yml = extras.get("schema_yml")
    if not isinstance(schema_yml, dict):
        return False
    sources = schema_yml.get("sources")
    if not isinstance(sources, list):
        return False
    for source in sources:
        if isinstance(source, dict) and source.get("freshness"):
            return True
        if isinstance(source, dict):
            for tbl in source.get("tables") or []:
                if isinstance(tbl, dict) and tbl.get("freshness"):
                    return True
    return False


def _coverage_gaps(pr: ParseResult, issues: List[Issue]) -> List[str]:
    """Human-readable monitoring gaps from observability issues + IR signals."""
    gaps: List[str] = []
    seen: Set[str] = set()

    def add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            gaps.append(text)

    fired = {i.rule for i in issues}
    for issue in issues:
        if issue.category == "observability":
            add(issue.message)

    ir = pr.ir
    if (
        "NO_SLA" not in fired
        and ir.format in ("airflow", "prefect")
        and not ir.scheduling.sla_minutes
    ):
        add(
            "No SLA configured — late or hung runs will not be detected until "
            "downstream consumers notice missing data."
        )

    read_names = sorted({t.name for t in ir.tables if t.access_type == "read" and t.name})
    if read_names and "UNTESTED_SOURCE" not in fired and not _has_existing_tests(pr):
        listed = ", ".join(read_names[:5])
        add(
            f"Upstream source(s) {listed} have no data tests — schema or volume "
            "drift upstream will propagate silently into this pipeline."
        )

    if (
        read_names
        and "MISSING_SOURCE_FRESHNESS" not in fired
        and not _has_freshness_checks(pr)
    ):
        add(
            "No freshness checks on input sources — stale upstream data will "
            "flow downstream undetected."
        )

    write_names = [t.name for t in ir.tables if t.access_type in ("write", "readwrite") and t.name]
    for name in dict.fromkeys(write_names):  # dedupe, keep order
        if len([g for g in gaps if "row-count anomaly" in g]) >= MAX_ROWCOUNT_GAP_TABLES:
            break
        add(
            f"No row-count anomaly monitoring on output table '{name}' — a "
            "silent upstream drop would publish empty or partial data."
        )

    return gaps[:MAX_COVERAGE_GAPS]


# ---------------------------------------------------------------------------
# Serialization with round-trip validation
# ---------------------------------------------------------------------------
def _render_dbt_yaml(doc: Dict[str, Any]) -> str:
    """Dump the dbt schema doc to YAML and verify it parses back cleanly."""
    rendered = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=100)
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict) or parsed.get("version") != 2:
        raise ValueError("generated dbt YAML failed round-trip validation")
    return rendered


def _render_ge_json(doc: Dict[str, Any]) -> str:
    """Dump the GE suite to pretty JSON and verify it parses back cleanly."""
    rendered = json.dumps(doc, indent=2, ensure_ascii=False)
    parsed = json.loads(rendered)
    if not isinstance(parsed, dict) or "expectations" not in parsed:
        raise ValueError("generated GE suite JSON failed round-trip validation")
    return rendered


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def generate_tests(pr: ParseResult, issues: List[Issue]) -> GeneratedTests:
    """Generate dbt + Great Expectations tests and coverage gaps for a pipeline.

    Deterministic and tolerant of sparse IR: tables without known columns are
    skipped, and a malformed parse result degrades to empty-but-valid
    artifacts rather than raising. Both ``dbt_yaml`` and
    ``great_expectations_json`` are guaranteed to parse (round-trip validated
    before returning).

    Args:
        pr: The parse result whose IR (tables, columns, lineage, scheduling,
            extras) drives test generation.
        issues: Rule-engine findings; observability-category issues seed the
            ``coverage_gaps`` list.

    Returns:
        A fully populated :class:`~app.schemas.report.GeneratedTests`.
    """
    issues = issues or []

    try:
        dbt_doc, ge_doc, tests = _build_artifacts(pr)
    except Exception:
        logger.exception("Test generation failed; returning empty artifacts")
        dbt_doc, ge_doc, tests = {"version": 2, "models": []}, None, []

    try:
        dbt_yaml = _render_dbt_yaml(dbt_doc)
    except Exception:
        logger.exception("dbt YAML rendering failed; falling back to empty block")
        dbt_yaml = _EMPTY_DBT_YAML

    try:
        ge_json = _render_ge_json(ge_doc) if ge_doc is not None else _EMPTY_GE_SUITE
    except Exception:
        logger.exception("GE suite rendering failed; falling back to empty suite")
        ge_json = _EMPTY_GE_SUITE

    try:
        coverage_gaps = _coverage_gaps(pr, issues)
    except Exception:
        logger.exception("Coverage-gap derivation failed; returning none")
        coverage_gaps = []

    logger.debug(
        "Generated %d tests across dbt+GE with %d coverage gaps", len(tests), len(coverage_gaps)
    )
    return GeneratedTests(
        dbt_yaml=dbt_yaml,
        great_expectations_json=ge_json,
        tests=tests,
        coverage_gaps=coverage_gaps,
    )
