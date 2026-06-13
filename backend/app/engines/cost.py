"""Warehouse-aware cost estimation engine.

Translates the IR's logical work (rows x row-width x table scans) plus the
rule engine's findings into real dollar estimates for Snowflake, BigQuery,
Redshift and Databricks, with transparent step-by-step reasoning.

Model
-----
1.  Baseline bytes/run = ``row_count * row_bytes * width_factor * table_reads``
    where ``row_bytes`` defaults to :data:`DEFAULT_ROW_BYTES` and
    ``width_factor`` shrinks when the query projects fewer columns than an
    assumed full-width table (``SELECT *`` keeps the full width).
2.  Cost-driving issues (matched by ``Issue.rule`` against
    :data:`RULE_COST_MULTIPLIERS`) compound into a work multiplier, capped at
    :data:`MAX_TOTAL_MULTIPLIER` so extreme inputs stay finite and plausible.
3.  Effective bytes are priced per warehouse using the documented constants
    below; the optimized path re-prices the baseline with every cost-driving
    multiplier removed, so ``optimized <= current`` always holds.

All public entry points are pure functions of their arguments — deterministic,
monotonic in ``row_count`` and ``daily_runs``, never negative/NaN/inf.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Tuple

from app.schemas.ir import ParseResult
from app.schemas.report import CostAnalysis, CostProjections, Issue, RiskLevel

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Work-model constants
# --------------------------------------------------------------------------
#: Assumed average analytic row width in bytes when nothing better is known.
DEFAULT_ROW_BYTES: float = 200.0
#: Assumed full-table column count used to scale projection width.
ASSUMED_TABLE_COLUMNS: int = 20
#: A projection can never shrink the scan below 15% of full width
#: (columnar engines still touch metadata, keys, and filter columns).
MIN_WIDTH_FACTOR: float = 0.15
#: Cap on the number of distinct table reads counted as separate scans.
TABLE_SCAN_CAP: int = 16
#: Hard ceiling on the compounded issue multiplier (keeps 5B-row pathological
#: inputs finite while remaining monotonic).
MAX_TOTAL_MULTIPLIER: float = 500.0
#: UNOPTIMIZED_CTE_REUSE compounds per re-scan, capped at this many re-scans.
MAX_CTE_RESCANS: int = 4
#: Input clamps — generous, but rule out absurd/overflowing parameters.
ROW_COUNT_CAP: int = 10**13
DAILY_RUNS_CAP: int = 100_000

# --------------------------------------------------------------------------
# Per-warehouse pricing constants (sources / assumptions documented inline)
# --------------------------------------------------------------------------
#: Snowflake: calibrated so a clean 10M-row x 200B (~2 GB) hourly job lands at
#: ~0.025 credits/run (inside the 0.01-0.05 target band for an XS warehouse).
SNOWFLAKE_BYTES_PER_CREDIT: float = 80e9
#: Snowflake Enterprise edition list price per credit (USD).
SNOWFLAKE_CREDIT_USD: float = 3.00

#: BigQuery on-demand analysis pricing: USD per TiB of bytes billed.
BIGQUERY_USD_PER_TIB: float = 6.25
#: BigQuery bills a minimum of 10 MiB per query that scans any data.
BIGQUERY_MIN_BYTES_BILLED: int = 10 * 1024 * 1024
TIB: int = 1024**4

#: Redshift ra3.4xlarge on-demand node price (USD/hour, us-east-1).
REDSHIFT_NODE_USD_PER_HOUR: float = 3.26
#: Effective per-query scan throughput on a shared ra3.4xlarge (bytes/sec).
REDSHIFT_SCAN_BYTES_PER_SEC: float = 300e6
#: Fixed planner/queue/commit overhead per query (seconds).
REDSHIFT_QUERY_OVERHEAD_SEC: float = 15.0
#: A single query occupies roughly a quarter of the node's concurrency slots.
REDSHIFT_CONCURRENCY_SHARE: float = 0.25

#: Databricks jobs-compute premium tier list price (USD per DBU).
DATABRICKS_USD_PER_DBU: float = 0.55
#: Small jobs cluster (driver + 2 workers) burns roughly 4 DBU per hour.
DATABRICKS_DBU_PER_HOUR: float = 4.0
#: Rough cloud-VM cost share for that same cluster (USD/hour).
DATABRICKS_VM_USD_PER_HOUR: float = 1.20
#: Effective scan throughput for the small jobs cluster (bytes/sec).
DATABRICKS_SCAN_BYTES_PER_SEC: float = 400e6
#: Amortized cluster spin-up overhead per job run (seconds).
DATABRICKS_JOB_OVERHEAD_SEC: float = 60.0

#: Warehouses this engine can price. Unknown strings fall back to snowflake.
SUPPORTED_WAREHOUSES: Tuple[str, ...] = ("snowflake", "bigquery", "redshift", "databricks")

# --------------------------------------------------------------------------
# Issue-driven work multipliers, keyed by rule id
# --------------------------------------------------------------------------
RULE_COST_MULTIPLIERS: Dict[str, float] = {
    "CARTESIAN_JOIN": 10.0,  # midpoint of the 8-12x blow-up band
    "EXPLODING_JOIN": 3.0,
    "UNBOUNDED_FULL_SCAN": 2.5,
    "MISSING_PARTITION_FILTER": 2.0,  # 4x on BigQuery (see below)
    "SELECT_STAR": 1.4,  # width penalty
    "UNOPTIMIZED_CTE_REUSE": 1.5,  # compounds per re-scan
    "WINDOW_FUNCTION_ON_FULL_TABLE": 1.6,
    "CORRELATED_SUBQUERY": 1.8,
    "NESTED_LOOP_RISK": 2.0,
}
#: On BigQuery, partition pruning IS the billing model — missing a partition
#: filter quadruples the bytes billed rather than merely doubling runtime.
BIGQUERY_PARTITION_FILTER_MULTIPLIER: float = 4.0

# Risk thresholds on current monthly spend (USD).
RISK_HIGH_MONTHLY_USD: float = 500.0
RISK_MEDIUM_MONTHLY_USD: float = 50.0


# --------------------------------------------------------------------------
# Small numeric helpers
# --------------------------------------------------------------------------
def _finite(value: float, default: float = 0.0) -> float:
    """Return ``value`` if it is a finite float, else ``default``."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _money(value: float) -> float:
    """Clamp to a finite, non-negative dollar amount rounded to cents."""
    return round(max(0.0, _finite(value)), 2)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]``, treating non-finite input as ``lo``."""
    return min(hi, max(lo, _finite(value, lo)))


def _fmt_bytes(num_bytes: float) -> str:
    """Human-readable byte count (e.g. ``'2.00 GB'``)."""
    num_bytes = max(0.0, _finite(num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1000.0 or unit == "TB":
            return f"{num_bytes:,.2f} {unit}"
        num_bytes /= 1000.0
    return f"{num_bytes:,.2f} TB"  # pragma: no cover - loop always returns


def normalize_warehouse(warehouse: Optional[str]) -> str:
    """Normalize a warehouse name; unknown values fall back to ``snowflake``."""
    name = (warehouse or "").strip().lower()
    if name not in SUPPORTED_WAREHOUSES:
        if name:
            logger.warning("Unknown warehouse %r — falling back to snowflake", warehouse)
        return "snowflake"
    return name


# --------------------------------------------------------------------------
# Step 1: baseline work estimate from the IR
# --------------------------------------------------------------------------
def _projection_width(pr: Optional[ParseResult]) -> Tuple[float, Optional[int], bool]:
    """Infer projection width from the IR.

    Returns ``(width_factor, projected_columns, is_select_star)`` where
    ``width_factor`` scales :data:`DEFAULT_ROW_BYTES` by projected column
    count vs an assumed :data:`ASSUMED_TABLE_COLUMNS`-column table. A
    ``SELECT *`` (or an unparseable projection) keeps the full width of 1.0.
    """
    if pr is None or getattr(pr, "ir", None) is None:
        return 1.0, None, False

    is_star = False
    projected: set = set()
    try:
        for op in pr.ir.ops("SELECT"):
            details = op.details or {}
            cols = details.get("columns")
            if details.get("star") or details.get("select_star") or details.get("is_star"):
                is_star = True
            if isinstance(cols, (list, tuple)):
                for col in cols:
                    if isinstance(col, str):
                        if col.strip() == "*" or col.strip().endswith(".*"):
                            is_star = True
                        else:
                            projected.add(col.strip().lower())
        for cl in pr.ir.column_lineage:
            if cl.output_column and cl.output_column != "*":
                projected.add(cl.output_column.lower())
    except Exception:  # pragma: no cover - malformed IR must not kill costing
        logger.exception("Failed to inspect projection width; assuming full width")
        return 1.0, None, False

    if is_star or not projected:
        return 1.0, None, is_star
    count = len(projected)
    factor = _clamp(count / float(ASSUMED_TABLE_COLUMNS), MIN_WIDTH_FACTOR, 1.0)
    return factor, count, False


def _table_read_count(pr: Optional[ParseResult]) -> int:
    """Number of distinct table scans, in ``[1, TABLE_SCAN_CAP]``."""
    if pr is None or getattr(pr, "ir", None) is None:
        return 1
    try:
        reads = {t.name for t in pr.ir.tables_by_access("read") if t.name}
        return int(_clamp(len(reads) or 1, 1, TABLE_SCAN_CAP))
    except Exception:  # pragma: no cover
        logger.exception("Failed to count table reads; assuming 1")
        return 1


def baseline_bytes_per_run(pr: Optional[ParseResult], row_count: int) -> Tuple[float, int, float, Optional[int]]:
    """Estimate clean (issue-free) bytes scanned per run from the IR.

    Returns ``(bytes_per_run, table_reads, width_factor, projected_columns)``.
    """
    rows = _clamp(row_count, 0, ROW_COUNT_CAP)
    width_factor, projected_cols, _ = _projection_width(pr)
    table_reads = _table_read_count(pr)
    bytes_per_run = rows * DEFAULT_ROW_BYTES * width_factor * table_reads
    return _finite(bytes_per_run), table_reads, width_factor, projected_cols


# --------------------------------------------------------------------------
# Step 2: issue-driven multipliers
# --------------------------------------------------------------------------
def issue_multipliers(issues: Optional[List[Issue]], warehouse: str) -> Dict[str, float]:
    """Per-rule cost multipliers triggered by the given issues.

    Every cost-driving rule contributes once, except ``UNOPTIMIZED_CTE_REUSE``
    which compounds per re-scan (capped at :data:`MAX_CTE_RESCANS`). On
    BigQuery, ``MISSING_PARTITION_FILTER`` uses the 4x billing multiplier.
    """
    contributions: Dict[str, float] = {}
    if not issues:
        return contributions
    counts: Dict[str, int] = {}
    for issue in issues:
        rule = getattr(issue, "rule", None)
        if isinstance(rule, str) and rule in RULE_COST_MULTIPLIERS:
            counts[rule] = counts.get(rule, 0) + 1
    for rule, count in counts.items():
        if rule == "UNOPTIMIZED_CTE_REUSE":
            rescans = min(count, MAX_CTE_RESCANS)
            contributions[rule] = RULE_COST_MULTIPLIERS[rule] ** rescans
        elif rule == "MISSING_PARTITION_FILTER" and warehouse == "bigquery":
            contributions[rule] = BIGQUERY_PARTITION_FILTER_MULTIPLIER
        else:
            contributions[rule] = RULE_COST_MULTIPLIERS[rule]
    return contributions


def _total_multiplier(contributions: Dict[str, float]) -> float:
    """Compound per-rule contributions, capped at :data:`MAX_TOTAL_MULTIPLIER`."""
    total = 1.0
    for mult in contributions.values():
        total *= max(1.0, _finite(mult, 1.0))
    return _clamp(total, 1.0, MAX_TOTAL_MULTIPLIER)


# --------------------------------------------------------------------------
# Step 3: per-warehouse pricing
# --------------------------------------------------------------------------
def price_per_run(warehouse: str, bytes_scanned: float) -> Tuple[float, float, int, str]:
    """Price one run on ``warehouse``.

    Returns ``(usd_per_run, credits_per_run, bytes_billed, pricing_line)``.
    ``credits_per_run`` is non-zero only for snowflake; ``bytes_billed`` only
    for bigquery. Zero bytes price to zero (overhead floors are skipped so an
    empty pipeline costs nothing).
    """
    bytes_scanned = max(0.0, _finite(bytes_scanned))
    if bytes_scanned <= 0.0:
        return 0.0, 0.0, 0, f"{warehouse}: no scannable work detected — $0.00/run."

    if warehouse == "bigquery":
        billed = int(max(bytes_scanned, BIGQUERY_MIN_BYTES_BILLED))
        usd = billed / TIB * BIGQUERY_USD_PER_TIB
        line = (
            f"bigquery on-demand: {_fmt_bytes(billed)} billed x "
            f"${BIGQUERY_USD_PER_TIB:.2f}/TiB = ${usd:,.4f}/run."
        )
        return _finite(usd), 0.0, billed, line

    if warehouse == "redshift":
        runtime_sec = REDSHIFT_QUERY_OVERHEAD_SEC + bytes_scanned / REDSHIFT_SCAN_BYTES_PER_SEC
        usd = (runtime_sec / 3600.0) * REDSHIFT_NODE_USD_PER_HOUR * REDSHIFT_CONCURRENCY_SHARE
        line = (
            f"redshift: ~{runtime_sec:,.0f}s on ra3.4xlarge "
            f"(${REDSHIFT_NODE_USD_PER_HOUR:.2f}/hr, "
            f"{REDSHIFT_CONCURRENCY_SHARE:.0%} concurrency share) = ${usd:,.4f}/run."
        )
        return _finite(usd), 0.0, 0, line

    if warehouse == "databricks":
        runtime_hr = (
            DATABRICKS_JOB_OVERHEAD_SEC + bytes_scanned / DATABRICKS_SCAN_BYTES_PER_SEC
        ) / 3600.0
        dbus = runtime_hr * DATABRICKS_DBU_PER_HOUR
        usd = dbus * DATABRICKS_USD_PER_DBU + runtime_hr * DATABRICKS_VM_USD_PER_HOUR
        line = (
            f"databricks jobs compute: ~{dbus:,.4f} DBU x "
            f"${DATABRICKS_USD_PER_DBU:.2f}/DBU + ~${runtime_hr * DATABRICKS_VM_USD_PER_HOUR:,.4f} "
            f"VM share = ${usd:,.4f}/run."
        )
        return _finite(usd), 0.0, 0, line

    # snowflake (and the fallback path)
    credits = bytes_scanned / SNOWFLAKE_BYTES_PER_CREDIT
    usd = credits * SNOWFLAKE_CREDIT_USD
    line = (
        f"snowflake: {_fmt_bytes(bytes_scanned)} of work ~ {credits:,.4f} credits/run x "
        f"${SNOWFLAKE_CREDIT_USD:.2f}/credit (Enterprise list) = ${usd:,.4f}/run."
    )
    return _finite(usd), _finite(credits), 0, line


# --------------------------------------------------------------------------
# Reasoning text
# --------------------------------------------------------------------------
def _describe_multiplier(rule: str, mult: float, rows: int) -> str:
    """One human-readable sentence explaining a rule's cost contribution."""
    blown_up = int(min(rows * mult, float(ROW_COUNT_CAP)))
    if rule == "CARTESIAN_JOIN":
        return (
            f"CARTESIAN_JOIN multiplies scanned rows ~{mult:g}x: "
            f"{rows:,} rows -> ~{blown_up:,} intermediate rows."
        )
    if rule == "EXPLODING_JOIN":
        return (
            f"EXPLODING_JOIN fans out matches ~{mult:g}x: "
            f"{rows:,} rows -> ~{blown_up:,} joined rows to shuffle."
        )
    if rule == "UNBOUNDED_FULL_SCAN":
        return (
            f"UNBOUNDED_FULL_SCAN forces a {mult:g}x full-history read "
            f"instead of an incremental slice over {rows:,} rows."
        )
    if rule == "MISSING_PARTITION_FILTER":
        return (
            f"MISSING_PARTITION_FILTER disables partition pruning: "
            f"~{mult:g}x more data read across {rows:,} rows per scan."
        )
    if rule == "SELECT_STAR":
        return (
            f"SELECT_STAR width penalty: every column is materialized, "
            f"~{mult:g}x more bytes per row than a trimmed projection."
        )
    if rule == "UNOPTIMIZED_CTE_REUSE":
        return (
            f"UNOPTIMIZED_CTE_REUSE re-executes the CTE on each reference: "
            f"~{mult:g}x total re-scan work over {rows:,} rows."
        )
    if rule == "WINDOW_FUNCTION_ON_FULL_TABLE":
        return (
            f"WINDOW_FUNCTION_ON_FULL_TABLE sorts/partitions the entire "
            f"{rows:,}-row set: ~{mult:g}x compute."
        )
    if rule == "CORRELATED_SUBQUERY":
        return (
            f"CORRELATED_SUBQUERY re-evaluates per outer row across "
            f"{rows:,} rows: ~{mult:g}x work."
        )
    if rule == "NESTED_LOOP_RISK":
        return (
            f"NESTED_LOOP_RISK degrades the join to row-at-a-time lookups "
            f"over {rows:,} rows: ~{mult:g}x compute."
        )
    return f"{rule} adds a ~{mult:g}x cost multiplier."  # pragma: no cover - exhaustive above


def _build_reasoning(
    *,
    warehouse: str,
    rows: int,
    table_reads: int,
    width_factor: float,
    projected_cols: Optional[int],
    base_bytes: float,
    contributions: Dict[str, float],
    total_multiplier: float,
    pricing_line: str,
    daily_runs: int,
    monthly_current: float,
    monthly_optimized: float,
    savings_pct: float,
) -> List[str]:
    """Assemble 4-8 transparent reasoning lines citing real numbers."""
    lines: List[str] = []

    width_note = ""
    if width_factor < 1.0 and projected_cols is not None:
        width_note = (
            f" (projection trimmed to {projected_cols} column(s), "
            f"{width_factor:.0%} of assumed {ASSUMED_TABLE_COLUMNS}-column width)"
        )
    lines.append(
        f"Baseline work: {rows:,} rows x {DEFAULT_ROW_BYTES * width_factor:.0f} bytes/row x "
        f"{table_reads} table read(s) ~ {_fmt_bytes(base_bytes)} scanned per run{width_note}."
    )

    ranked = sorted(contributions.items(), key=lambda kv: (-kv[1], kv[0]))
    for rule, mult in ranked[:3]:
        lines.append(_describe_multiplier(rule, mult, rows))
    if len(ranked) > 3:
        lines.append(
            f"...plus {len(ranked) - 3} more cost-driving issue(s); all findings "
            f"compound to a {total_multiplier:g}x work multiplier overall."
        )

    lines.append(pricing_line)
    lines.append(
        f"{daily_runs} run(s)/day x 30 days: ${monthly_current:,.2f}/month now vs "
        f"${monthly_optimized:,.2f}/month after fixes ({savings_pct:.1f}% savings)."
    )

    if ranked:
        top_rule, top_mult = ranked[0]
        remaining = {r: m for r, m in contributions.items() if r != top_rule}
        per_run_without, _, _, _ = price_per_run(
            warehouse, base_bytes * _total_multiplier(remaining)
        )
        monthly_without = _money(per_run_without * daily_runs * 30)
        delta = _money(monthly_current - monthly_without)
        lines.append(
            f"Highest-ROI fix: resolve {top_rule} first — it alone carries a "
            f"{top_mult:g}x multiplier, worth ~${delta:,.2f}/month."
        )
    else:
        lines.append(
            "No cost-driving issues detected — this pipeline is already near "
            "its optimal scan cost."
        )
    return lines[:8]


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def estimate_cost(
    pr: ParseResult,
    issues: List[Issue],
    *,
    row_count: int = 10_000_000,
    daily_runs: int = 24,
    warehouse: str = "snowflake",
) -> CostAnalysis:
    """Estimate current vs optimized run cost for a parsed pipeline.

    Args:
        pr: Parse result whose IR drives the baseline work estimate.
        issues: Rule-engine findings; rules listed in
            :data:`RULE_COST_MULTIPLIERS` inflate the current-cost path.
        row_count: Rows processed per run (clamped to ``[0, 10^13]``).
        daily_runs: Runs per day (clamped to ``[0, 100000]``).
        warehouse: One of snowflake / bigquery / redshift / databricks;
            anything else falls back to snowflake.

    Returns:
        A fully populated :class:`CostAnalysis` with deterministic dollar
        math, 30/90/365-day projections for both paths, a risk level, and
        4-8 human reasoning lines. Guarantees ``optimized <= current``, no
        negative/NaN/inf values, and currency rounded to cents.
    """
    wh = normalize_warehouse(warehouse)
    rows = int(_clamp(row_count, 0, ROW_COUNT_CAP))
    runs = int(_clamp(daily_runs, 0, DAILY_RUNS_CAP))

    base_bytes, table_reads, width_factor, projected_cols = baseline_bytes_per_run(pr, rows)
    contributions = issue_multipliers(issues, wh)
    total_mult = _total_multiplier(contributions)
    effective_bytes = base_bytes * total_mult

    per_run_current, credits_per_run, bytes_billed, pricing_line = price_per_run(
        wh, effective_bytes
    )
    per_run_optimized, _, _, _ = price_per_run(wh, base_bytes)
    per_run_optimized = min(per_run_optimized, per_run_current)

    monthly_current = _money(per_run_current * runs * 30)
    monthly_optimized = min(_money(per_run_optimized * runs * 30), monthly_current)

    if monthly_current > 0:
        savings_pct = round(
            _clamp((monthly_current - monthly_optimized) / monthly_current * 100.0, 0.0, 100.0),
            1,
        )
    else:
        savings_pct = 0.0

    if monthly_current > RISK_HIGH_MONTHLY_USD:
        risk_level: RiskLevel = "HIGH"
    elif monthly_current > RISK_MEDIUM_MONTHLY_USD:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    projections = CostProjections(
        d30_current=monthly_current,
        d90_current=_money(per_run_current * runs * 90),
        d365_current=_money(per_run_current * runs * 365),
        d30_optimized=monthly_optimized,
        d90_optimized=min(_money(per_run_optimized * runs * 90), _money(per_run_current * runs * 90)),
        d365_optimized=min(_money(per_run_optimized * runs * 365), _money(per_run_current * runs * 365)),
    )

    reasoning = _build_reasoning(
        warehouse=wh,
        rows=rows,
        table_reads=table_reads,
        width_factor=width_factor,
        projected_cols=projected_cols,
        base_bytes=base_bytes,
        contributions=contributions,
        total_multiplier=total_mult,
        pricing_line=pricing_line,
        daily_runs=runs,
        monthly_current=monthly_current,
        monthly_optimized=monthly_optimized,
        savings_pct=savings_pct,
    )

    logger.debug(
        "Cost estimate: warehouse=%s rows=%d runs/day=%d base=%.0fB mult=%.2f "
        "current=$%.2f/mo optimized=$%.2f/mo",
        wh, rows, runs, base_bytes, total_mult, monthly_current, monthly_optimized,
    )

    return CostAnalysis(
        provider=wh,  # type: ignore[arg-type]  # normalized to the Warehouse literal set
        credits_per_run=round(max(0.0, _finite(credits_per_run)), 4),
        bytes_billed=max(0, int(bytes_billed)),
        monthly_usd_current=monthly_current,
        monthly_usd_optimized=monthly_optimized,
        savings_pct=savings_pct,
        risk_level=risk_level,
        projections=projections,
        reasoning=reasoning,
    )
