"""dbt model rules.

Fifteen deterministic checks over the IR + extras produced by
``app.parsers.dbt_parser``. The parser puts the parsed ``schema.yml`` document
into ``extras["schema_yml"]``, ``{{ ref() }}`` targets into ``extras["refs"]``,
``{{ source() }}`` targets into ``extras["sources"]``, declared tests into
``extras["tests"]``, the configured ``unique_key`` into
``extras["unique_key"]`` and hardcoded (non-ref/source) table references into
``extras["raw_table_refs"]``. Every rule reads these defensively — extras may
be partially populated for malformed input — and falls back to the IR
(materialization, metadata, operations) where possible.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, FrozenSet, List, Optional, Set

from app.rules import Rule, register
from app.schemas.ir import ParseResult
from app.schemas.report import Issue

logger = logging.getLogger(__name__)

DBT_ONLY: FrozenSet[str] = frozenset({"dbt"})

#: Models with at least this many joins deserve decomposition.
JOIN_THRESHOLD = 6

#: Accepted dbt model-name prefixes (layered naming convention).
NAMING_PREFIXES = ("stg_", "int_", "fct_", "dim_", "mart")

_ID_LIKE = re.compile(r"(?:^id$|_id$|_key$|^pk_|_pk$)", re.IGNORECASE)
_DATEISH = re.compile(
    r"(date|_at\b|_ts\b|time|partition|\bdt\b|\bday\b|\bmonth\b)", re.IGNORECASE
)
_IS_INCREMENTAL_CALL = re.compile(r"is_incremental\s*\(")
_EPHEMERAL_CONFIG = re.compile(r"materialized\s*=\s*['\"]ephemeral['\"]", re.IGNORECASE)
_TEST_CALL = re.compile(r"^([\w.]+)\s*\(\s*([\w.]+)\s*\)$")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _schema_yml(pr: ParseResult) -> Dict[str, Any]:
    """The parsed ``schema.yml`` document, or ``{}`` when absent/malformed."""
    raw = pr.extras.get("schema_yml")
    return raw if isinstance(raw, dict) else {}


def _config(pr: ParseResult) -> Dict[str, Any]:
    """The model's ``{{ config(...) }}`` kwargs, or ``{}`` when absent."""
    raw = pr.extras.get("config")
    return raw if isinstance(raw, dict) else {}


def _as_list(value: Any) -> List[Any]:
    """Normalize a YAML scalar/list/None into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _resolved_model_name(pr: ParseResult) -> Optional[str]:
    """The model's name from extras, IR metadata or the written table."""
    name = pr.extras.get("model_name") or (
        pr.ir.metadata.name if pr.ir.metadata else None
    )
    if isinstance(name, str) and name.strip():
        return name.strip()
    for table in pr.ir.tables:
        if table.access_type in ("write", "readwrite") and table.name:
            return table.name
    return None


def _model_name(pr: ParseResult) -> str:
    """Model name for messages, with a readable fallback."""
    return _resolved_model_name(pr) or "this model"


def _model_entry(pr: ParseResult) -> Dict[str, Any]:
    """The ``schema.yml`` models entry for this model (or the first one)."""
    models = _schema_yml(pr).get("models")
    if not isinstance(models, list):
        return {}
    wanted = (_resolved_model_name(pr) or "").lower()
    for entry in models:
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == wanted:
            return entry
    for entry in models:
        if isinstance(entry, dict):
            return entry
    return {}


def _materialized(pr: ParseResult) -> str:
    """Effective materialization: IR first, then config, else ``unknown``."""
    mat = pr.ir.materialization
    if mat is not None and mat.type and mat.type != "unknown":
        return str(mat.type).lower()
    config_mat = _config(pr).get("materialized")
    if isinstance(config_mat, str) and config_mat.strip():
        return config_mat.strip().lower()
    return "unknown"


def _incremental_strategy(pr: ParseResult) -> Optional[str]:
    """The configured incremental strategy, if any."""
    mat = pr.ir.materialization
    if mat is not None and isinstance(mat.strategy, str) and mat.strategy.strip():
        return mat.strategy.strip()
    strategy = _config(pr).get("incremental_strategy")
    if isinstance(strategy, str) and strategy.strip():
        return strategy.strip()
    return None


def _unique_key(pr: ParseResult) -> Optional[str]:
    """The configured ``unique_key`` (first column when a list)."""
    for raw in (pr.extras.get("unique_key"), _config(pr).get("unique_key")):
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, (list, tuple)) and raw:
            first = raw[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return None


def _first_output_column(pr: ParseResult) -> Optional[str]:
    """The model's first output column, from IR tables, schema.yml or lineage."""
    for table in pr.ir.tables:
        if table.access_type in ("write", "readwrite") and table.columns:
            return str(table.columns[0])
    columns = _model_entry(pr).get("columns")
    if isinstance(columns, list):
        for col in columns:
            if isinstance(col, dict) and col.get("name"):
                return str(col["name"])
    if pr.ir.column_lineage:
        return pr.ir.column_lineage[0].output_column
    return None


def _primary_key_column(pr: ParseResult) -> Optional[str]:
    """The model's primary-key column: ``unique_key`` or an id-like first column."""
    key = _unique_key(pr)
    if key:
        return key
    first = _first_output_column(pr)
    if first and _ID_LIKE.search(first):
        return first
    return None


def _normalized_test_name(test: Any) -> Optional[str]:
    """Lowercased test name from a YAML test entry (string or one-key dict)."""
    if isinstance(test, str):
        name = test.strip().lower()
        return name or None
    if isinstance(test, dict) and test:
        key = next(iter(test))
        name = str(key).strip().lower()
        return name or None
    return None


def _collect_column_tests(pr: ParseResult) -> Dict[str, Set[str]]:
    """Map of lowercase column name -> set of lowercase test names.

    Model-level tests are stored under the ``""`` key. Sources are
    ``extras["tests"]`` (normalized from several plausible shapes) plus the
    ``models`` section of ``schema.yml``.
    """
    collected: Dict[str, Set[str]] = {}

    def add(column: Any, test: Any) -> None:
        name = _normalized_test_name(test)
        if name is None:
            return
        key = str(column).strip().lower() if column else ""
        collected.setdefault(key, set()).add(name)

    raw_tests = pr.extras.get("tests")
    if isinstance(raw_tests, list):
        for entry in raw_tests:
            if isinstance(entry, dict):
                column = (
                    entry.get("column")
                    or entry.get("column_name")
                    or entry.get("col")
                )
                test = (
                    entry.get("test")
                    or entry.get("test_name")
                    or entry.get("type")
                    or entry.get("name")
                )
                if test is None and column is None and len(entry) == 1:
                    key, value = next(iter(entry.items()))
                    if isinstance(value, list):  # {"order_id": ["unique", ...]}
                        for item in value:
                            add(key, item)
                        continue
                    test = key  # {"unique": {...}}
                add(column, test)
            elif isinstance(entry, str):
                text = entry.strip()
                match = _TEST_CALL.match(text)  # "unique(order_id)"
                if match:
                    add(match.group(2), match.group(1))
                elif ":" in text:  # "order_id:unique"
                    column, test = text.split(":", 1)
                    add(column.strip(), test.strip())
                else:
                    add(None, text)

    models = _schema_yml(pr).get("models")
    if isinstance(models, list):
        for entry in models:
            if not isinstance(entry, dict):
                continue
            for test in _as_list(entry.get("tests")) + _as_list(entry.get("data_tests")):
                add("", test)
            columns = entry.get("columns")
            if not isinstance(columns, list):
                continue
            for col in columns:
                if not isinstance(col, dict):
                    continue
                for test in _as_list(col.get("tests")) + _as_list(col.get("data_tests")):
                    add(col.get("name"), test)
    return collected


def _all_test_names(pr: ParseResult) -> Set[str]:
    """Every test name declared anywhere for this model."""
    names: Set[str] = set()
    for tests in _collect_column_tests(pr).values():
        names.update(tests)
    return names


def _yaml_has_key(node: Any, key: str) -> bool:
    """Recursively check a YAML tree for a truthy value under ``key``."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and v:
                return True
            if _yaml_has_key(v, key):
                return True
    elif isinstance(node, list):
        return any(_yaml_has_key(item, key) for item in node)
    return False


def _yaml_has_tests(node: Any) -> bool:
    """Recursively check a YAML tree for any non-empty tests/data_tests block."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("tests", "data_tests") and v:
                return True
            if _yaml_has_tests(v):
                return True
    elif isinstance(node, list):
        return any(_yaml_has_tests(item) for item in node)
    return False


def _find_line(pr: ParseResult, pattern: "re.Pattern[str] | str") -> Optional[int]:
    """1-based line of the first source line matching ``pattern``."""
    rx = re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
    for number, text in enumerate(pr.lines, start=1):
        if rx.search(text):
            return number
    return None


def _config_line(pr: ParseResult) -> Optional[int]:
    """Line of the ``{{ config(...) }}`` block, if present in the source."""
    return _find_line(pr, r"\{\{\s*config\s*\(")


def _source_label(item: Any) -> str:
    """Readable label for a sources/raw-ref entry of unknown shape."""
    if isinstance(item, str):
        return item
    if isinstance(item, (list, tuple)):
        return ".".join(str(part) for part in item)
    if isinstance(item, dict):
        name = item.get("name") or item.get("table") or item.get("source")
        if name:
            return str(name)
    return str(item)


def _has_partition_filter(pr: ParseResult) -> bool:
    """True when the model filters on a partition/date-like predicate."""
    if pr.ir.ops("PARTITION_FILTER"):
        return True
    for op in pr.ir.ops("FILTER"):
        blob = " ".join(str(v) for v in (op.details or {}).values())
        if _DATEISH.search(blob):
            return True
    return False


# ---------------------------------------------------------------------------
# CRITICAL rules
# ---------------------------------------------------------------------------

@register
class NoTestsOnPrimaryKeyRule(Rule):
    """An untested primary key lets duplicates and nulls flow downstream."""

    id = "NO_TESTS_ON_PRIMARY_KEY"
    severity = "CRITICAL"
    category = "observability"
    formats = DBT_ONLY
    title = "Primary key has no tests"
    description = (
        "The model's primary-key column has neither a unique nor a not_null "
        "test, so duplicate or null keys silently corrupt every downstream "
        "join and metric."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag a unique_key/id-like key column with no unique/not_null test."""
        key = _primary_key_column(pr)
        if not key:
            return []
        tests = _collect_column_tests(pr).get(key.lower(), set())
        has_unique = any("unique" in name for name in tests)
        has_not_null = any("not_null" in name for name in tests)
        if has_unique or has_not_null:
            return []
        model = _model_name(pr)
        fix_diff = (
            "--- current\n"
            "+++ optimized\n"
            " columns:\n"
            f"   - name: {key}\n"
            "+    tests:\n"
            "+      - unique\n"
            "+      - not_null"
        )
        return [
            self.issue(
                f"Primary key column {key!r} of model {model!r} has no unique "
                "or not_null test — duplicate or null keys will reach every "
                "downstream join and metric undetected.",
                fix_suggestion=(
                    f"Add unique and not_null tests for {key!r} under the "
                    f"model's columns in schema.yml."
                ),
                fix_diff=fix_diff,
            )
        ]


@register
class UntestedSourceRule(Rule):
    """Sources are the trust boundary — they must carry tests."""

    id = "UNTESTED_SOURCE"
    severity = "CRITICAL"
    category = "observability"
    formats = DBT_ONLY
    title = "Sources used without any tests"
    description = (
        "The model reads from declared sources but the schema.yml defines no "
        "tests at all, so bad upstream data enters the project unchecked."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag source usage when schema.yml declares zero tests."""
        sources = pr.extras.get("sources")
        if not isinstance(sources, list) or not sources:
            return []
        if pr.extras.get("tests"):
            return []
        if _yaml_has_tests(_schema_yml(pr)):
            return []
        labels = ", ".join(_source_label(s) for s in sources[:5])
        extra = f" (+{len(sources) - 5} more)" if len(sources) > 5 else ""
        return [
            self.issue(
                f"Model {_model_name(pr)!r} reads from source(s) {labels}{extra} "
                "but its schema.yml defines no tests at all. Upstream schema "
                "drift or bad loads will flow straight through.",
                fix_suggestion=(
                    "Add at least not_null/unique tests on the source key "
                    "columns (and accepted_values where applicable) in "
                    "schema.yml so upstream regressions fail loudly."
                ),
            )
        ]


@register
class EphemeralInProductionRule(Rule):
    """Ephemeral models are inlined CTEs — undebuggable and recomputed."""

    id = "EPHEMERAL_IN_PRODUCTION"
    severity = "CRITICAL"
    category = "reliability"
    formats = DBT_ONLY
    title = "Ephemeral materialization in production"
    description = (
        "Ephemeral models are interpolated as CTEs into every consumer: they "
        "cannot be queried, tested or inspected, and their logic is recomputed "
        "by each downstream model."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag models materialized as ephemeral."""
        if _materialized(pr) != "ephemeral":
            return []
        model = _model_name(pr)
        line: Optional[int] = None
        old_line: Optional[str] = None
        for number, text in enumerate(pr.lines, start=1):
            if _EPHEMERAL_CONFIG.search(text):
                line, old_line = number, text
                break
        if line is None:
            line = _config_line(pr)
        if old_line is not None:
            new_line = old_line.replace("ephemeral", "view")
            fix_diff = f"--- current\n+++ optimized\n-{old_line}\n+{new_line}"
        else:
            fix_diff = (
                "--- current\n"
                "+++ optimized\n"
                "-{{ config(materialized='ephemeral') }}\n"
                "+{{ config(materialized='view') }}"
            )
        return [
            self.issue(
                f"Model {model!r} is materialized as ephemeral. It cannot be "
                "queried or tested directly, and every downstream model "
                "recompiles and re-executes its logic.",
                line=line,
                fix_suggestion=(
                    "Materialize the model as a view (cheap, queryable) or "
                    "table/incremental if it is reused by several consumers; "
                    "reserve ephemeral for trivial private helpers."
                ),
                fix_diff=fix_diff,
            )
        ]


# ---------------------------------------------------------------------------
# WARNING rules
# ---------------------------------------------------------------------------

@register
class MissingDescriptionRule(Rule):
    """Undocumented models rot fastest."""

    id = "MISSING_DESCRIPTION"
    severity = "WARNING"
    category = "maintainability"
    formats = DBT_ONLY
    title = "Model has no description"
    description = (
        "Neither schema.yml nor the model config carries a description, so "
        "dbt docs render an empty page and consumers must read SQL to learn "
        "what the model means."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag models without a description in schema.yml or metadata."""
        meta_desc = pr.ir.metadata.description if pr.ir.metadata else None
        entry_desc = _model_entry(pr).get("description")
        if (isinstance(meta_desc, str) and meta_desc.strip()) or (
            isinstance(entry_desc, str) and entry_desc.strip()
        ):
            return []
        model = _model_name(pr)
        return [
            self.issue(
                f"Model {model!r} has no description in schema.yml; dbt docs "
                "will render an empty page for it.",
                fix_suggestion=(
                    f"Add a description for {model!r} (and its key columns) in "
                    "schema.yml — one sentence on grain and purpose is enough."
                ),
            )
        ]


@register
class MissingIncrementalStrategyRule(Rule):
    """Implicit incremental strategies surprise people across warehouses."""

    id = "MISSING_INCREMENTAL_STRATEGY"
    severity = "WARNING"
    category = "performance"
    formats = DBT_ONLY
    title = "Incremental model without an explicit strategy"
    description = (
        "An incremental model with no incremental_strategy falls back to the "
        "adapter default (often append/delete+insert), which may duplicate "
        "rows or rewrite far more data than a merge would."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag incremental models lacking an incremental_strategy."""
        if _materialized(pr) != "incremental" or _incremental_strategy(pr):
            return []
        model = _model_name(pr)
        key = _unique_key(pr)
        key_line = f"+    unique_key='{key}',\n" if key else ""
        key_arg = f", unique_key='{key}'" if key else ""
        fix_diff = (
            "--- current\n"
            "+++ optimized\n"
            f"-{{{{ config(materialized='incremental'{key_arg}) }}}}\n"
            "+{{ config(\n"
            "+    materialized='incremental',\n"
            f"{key_line}"
            "+    incremental_strategy='merge',\n"
            "+) }}"
        )
        return [
            self.issue(
                f"Incremental model {model!r} does not set "
                "incremental_strategy, so it relies on the adapter default — "
                "behaviour (and cost) silently differs between warehouses.",
                line=_config_line(pr),
                fix_suggestion=(
                    "Set incremental_strategy explicitly ('merge' with a "
                    "unique_key for upserts, or 'insert_overwrite' for "
                    "partition-replacing loads)."
                ),
                fix_diff=fix_diff,
            )
        ]


@register
class FullRefreshOnLargeTableRule(Rule):
    """Rebuilding an unfiltered table every run burns money."""

    id = "FULL_REFRESH_ON_LARGE_TABLE"
    severity = "WARNING"
    category = "cost"
    formats = DBT_ONLY
    title = "Full-refresh table without a partition filter"
    description = (
        "The model is materialized as a plain table and its SQL has no "
        "partition/date filter, so every run rescans and rebuilds the full "
        "history even though only recent data changes."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag table-materialized models whose SQL has no partition filter."""
        if _materialized(pr) != "table" or _has_partition_filter(pr):
            return []
        model = _model_name(pr)
        return [
            self.issue(
                f"Model {model!r} is a full-refresh table with no partition or "
                "date filter — each run rescans and rewrites the entire "
                "history, and cost grows linearly with table age.",
                line=_config_line(pr),
                fix_suggestion=(
                    "Convert the model to materialized='incremental' with an "
                    "is_incremental() date filter and a unique_key, or at "
                    "minimum add a partition filter to bound the scan."
                ),
            )
        ]


@register
class MissingUniqueTestRule(Rule):
    """Without a unique test, silent fan-out goes unnoticed."""

    id = "MISSING_UNIQUE_TEST"
    severity = "WARNING"
    category = "observability"
    formats = DBT_ONLY
    title = "No unique test defined"
    description = (
        "No column of the model has a unique (or unique-combination) test, so "
        "an accidental join fan-out that duplicates rows is never caught."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag models with no unique-style test on any column."""
        if any("unique" in name for name in _all_test_names(pr)):
            return []
        model = _model_name(pr)
        key = _primary_key_column(pr) or _first_output_column(pr)
        target = f" on its key column {key!r}" if key else ""
        return [
            self.issue(
                f"Model {model!r} has no unique test{target}. Join fan-out "
                "that duplicates rows will pass every run unnoticed.",
                fix_suggestion=(
                    "Add a unique test on the grain column in schema.yml (or "
                    "dbt_utils.unique_combination_of_columns for composite "
                    "grains)."
                ),
            )
        ]


@register
class RefInsteadOfSourceRule(Rule):
    """Hardcoded table names break dbt's dependency graph."""

    id = "REF_INSTEAD_OF_SOURCE"
    severity = "WARNING"
    category = "maintainability"
    formats = DBT_ONLY
    title = "Hardcoded table reference"
    description = (
        "The model references warehouse tables by hardcoded name instead of "
        "{{ ref() }}/{{ source() }}, so dbt cannot order builds, draw lineage "
        "or relocate the table between environments."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag hardcoded table references reported by the parser."""
        raw_refs = pr.extras.get("raw_table_refs")
        if not isinstance(raw_refs, list) or not raw_refs:
            return []
        labels = [_source_label(r) for r in raw_refs]
        shown = ", ".join(labels[:5])
        extra = f" (+{len(labels) - 5} more)" if len(labels) > 5 else ""
        first_line = _find_line(pr, re.escape(labels[0])) if labels and labels[0] else None
        return [
            self.issue(
                f"Model {_model_name(pr)!r} references hardcoded table(s) "
                f"{shown}{extra} directly. These bypass dbt's DAG: builds are "
                "not ordered, lineage is broken, and dev/prod schemas cannot "
                "be swapped.",
                line=first_line,
                fix_suggestion=(
                    "Replace hardcoded names with {{ source('raw', 'table') }} "
                    "for external tables or {{ ref('model') }} for project "
                    "models, declaring the source in schema.yml."
                ),
            )
        ]


@register
class NoUniqueKeyOnIncrementalRule(Rule):
    """Incremental without unique_key appends duplicates on reprocessing."""

    id = "NO_UNIQUE_KEY_ON_INCREMENTAL"
    severity = "WARNING"
    category = "reliability"
    formats = DBT_ONLY
    title = "Incremental model without unique_key"
    description = (
        "An incremental model with no unique_key can only append; any rerun, "
        "backfill or late-arriving overlap inserts duplicate rows."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag incremental models lacking a unique_key."""
        if _materialized(pr) != "incremental" or _unique_key(pr):
            return []
        model = _model_name(pr)
        candidate = _first_output_column(pr)
        hint = f" (likely candidate: {candidate!r})" if candidate else ""
        return [
            self.issue(
                f"Incremental model {model!r} has no unique_key{hint}. "
                "Reruns and overlapping loads will append duplicate rows "
                "instead of updating existing ones.",
                line=_config_line(pr),
                fix_suggestion=(
                    "Set unique_key in the config to the model's grain column "
                    "so dbt merges instead of blindly appending."
                ),
            )
        ]


@register
class MissingNotNullTestRule(Rule):
    """Null keys are the quietest data-quality failure."""

    id = "MISSING_NOT_NULL_TEST"
    severity = "WARNING"
    category = "observability"
    formats = DBT_ONLY
    title = "No not_null test defined"
    description = (
        "No column of the model has a not_null test, so null identifiers or "
        "measures slip into downstream joins and aggregates silently."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag models with no not_null test on any column."""
        if any("not_null" in name for name in _all_test_names(pr)):
            return []
        model = _model_name(pr)
        key = _primary_key_column(pr) or _first_output_column(pr)
        target = f", starting with {key!r}" if key else ""
        return [
            self.issue(
                f"Model {model!r} has no not_null test on any column{target}. "
                "Null keys silently drop rows from inner joins downstream.",
                fix_suggestion=(
                    "Add not_null tests in schema.yml on the key column and "
                    "any column consumers join or filter on."
                ),
            )
        ]


@register
class MissingIncrementalGuardRule(Rule):
    """Incremental models must guard their delta filter with is_incremental()."""

    id = "MISSING_INCREMENTAL_GUARD"
    severity = "WARNING"
    category = "performance"
    formats = DBT_ONLY
    title = "Incremental model without is_incremental() guard"
    description = (
        "The model is materialized as incremental but never calls "
        "is_incremental(), so every run reprocesses the full source instead "
        "of just the new delta."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag incremental models whose SQL lacks an is_incremental() block."""
        if _materialized(pr) != "incremental":
            return []
        if _IS_INCREMENTAL_CALL.search(pr.source or ""):
            return []
        model = _model_name(pr)
        return [
            self.issue(
                f"Incremental model {model!r} never calls is_incremental(). "
                "Without the guard, every scheduled run scans and reprocesses "
                "the entire source table, erasing the benefit of incremental "
                "materialization.",
                line=_config_line(pr),
                fix_suggestion=(
                    "Wrap the delta filter in the guard, e.g. "
                    "{% if is_incremental() %} where updated_at > "
                    "(select max(updated_at) from {{ this }}) {% endif %}."
                ),
            )
        ]


@register
class TooManyJoinsRule(Rule):
    """A model that joins everything is a model nobody can change."""

    id = "TOO_MANY_JOINS"
    severity = "WARNING"
    category = "performance"
    formats = DBT_ONLY
    title = "Too many joins in one model"
    description = (
        "Six or more joins in a single model strain the optimizer, multiply "
        "fan-out risk and usually mean several intermediate models are hiding "
        "inside one file."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag models containing 6+ JOIN operations."""
        joins = pr.ir.ops("JOIN")
        if len(joins) < JOIN_THRESHOLD:
            return []
        model = _model_name(pr)
        marker = joins[JOIN_THRESHOLD - 1]
        line = marker.location.line if marker.location and marker.location.line else None
        return [
            self.issue(
                f"Model {model!r} performs {len(joins)} joins in a single "
                "query. Beyond ~5 joins the optimizer's plans degrade and the "
                "model becomes risky to modify.",
                line=line,
                fix_suggestion=(
                    "Split the query into staged intermediate models "
                    "(int_*) that pre-join related entities, then join the "
                    "narrow intermediates here."
                ),
            )
        ]


# ---------------------------------------------------------------------------
# INFO rules
# ---------------------------------------------------------------------------

@register
class NoTagsRule(Rule):
    """Tags drive selective runs and ownership filters."""

    id = "NO_TAGS"
    severity = "INFO"
    category = "maintainability"
    formats = DBT_ONLY
    title = "Model has no tags"
    description = (
        "Untagged models cannot be selected by domain or cadence "
        "(dbt run --select tag:...), which complicates partial deployments "
        "and ownership reporting."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag models with no tags in config, metadata or schema.yml."""
        if pr.ir.metadata and pr.ir.metadata.tags:
            return []
        if _as_list(_config(pr).get("tags")):
            return []
        if _as_list(_model_entry(pr).get("tags")):
            return []
        model = _model_name(pr)
        return [
            self.issue(
                f"Model {model!r} has no tags; tags enable selective builds "
                "like `dbt run --select tag:finance`.",
                line=_config_line(pr),
                fix_suggestion=(
                    "Add tags=['<domain>', '<cadence>'] to the model config "
                    "or its schema.yml entry."
                ),
            )
        ]


@register
class ModelNamingConventionRule(Rule):
    """Layer prefixes encode a model's place in the DAG."""

    id = "MODEL_NAMING_CONVENTION"
    severity = "INFO"
    category = "maintainability"
    formats = DBT_ONLY
    title = "Model name lacks a layer prefix"
    description = (
        "The model name does not follow the layered convention "
        "(stg_/int_/fct_/dim_/mart), so its role in the DAG is not obvious "
        "from its name."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag model names without a stg_/int_/fct_/dim_/mart prefix."""
        name = _resolved_model_name(pr)
        if not name:
            return []
        bare = name.split(".")[-1].lower()
        if bare.startswith(NAMING_PREFIXES):
            return []
        return [
            self.issue(
                f"Model name {name!r} does not follow the layered naming "
                "convention (stg_, int_, fct_, dim_ or mart prefixes), so its "
                "DAG layer is not obvious at a glance.",
                fix_suggestion=(
                    f"Rename the model with its layer prefix, e.g. "
                    f"'stg_{bare}' for a staging model or 'fct_{bare}' for a "
                    "fact."
                ),
            )
        ]


@register
class MissingSourceFreshnessRule(Rule):
    """Freshness checks catch stalled upstream loads before consumers do."""

    id = "MISSING_SOURCE_FRESHNESS"
    severity = "INFO"
    category = "observability"
    formats = DBT_ONLY
    title = "Sources without freshness checks"
    description = (
        "Declared sources have no freshness configuration, so a stalled "
        "upstream load is only discovered when consumers notice stale "
        "numbers."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag declared sources lacking any freshness configuration."""
        sources = pr.extras.get("sources")
        if not isinstance(sources, list) or not sources:
            return []
        if _yaml_has_key(_schema_yml(pr).get("sources"), "freshness"):
            return []
        labels = ", ".join(_source_label(s) for s in sources[:5])
        extra = f" (+{len(sources) - 5} more)" if len(sources) > 5 else ""
        return [
            self.issue(
                f"Source(s) {labels}{extra} have no freshness configuration. "
                "`dbt source freshness` cannot warn when the upstream load "
                "stalls.",
                fix_suggestion=(
                    "Add a freshness block (warn_after/error_after with a "
                    "loaded_at_field) to the source definitions in schema.yml."
                ),
            )
        ]
