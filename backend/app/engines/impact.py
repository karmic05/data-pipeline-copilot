"""Issue impact simulation engine.

Translates each detected :class:`~app.schemas.report.Issue` into concrete,
deterministic business impact numbers — extra latency, extra monthly warehouse
spend, per-run failure probability and projected PagerDuty incidents — given
the user's workload parameters (``row_count``, ``daily_runs``, ``warehouse``,
``base_monthly_cost``).

The heart of the module is :data:`IMPACT_MODELS`: a registry keyed by rule id
mapping to parameterized model functions. Rules without a bespoke model fall
back to a severity x category heuristic. All outputs are clamped and finite —
no NaN/inf ever escapes this module.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Dict, List, NamedTuple, Tuple

from app.schemas.report import ImpactResult, Issue, IssueImpact

logger = logging.getLogger(__name__)

#: Hard ceiling on any per-run failure probability we report.
_MAX_FAILURE_PROBABILITY = 0.95
#: Days used to convert per-day rates into monthly figures.
_DAYS_PER_MONTH = 30
#: Sanity ceiling on multipliers so a buggy model can never overflow output.
_MAX_MULTIPLIER = 1000.0


class ModelOutput(NamedTuple):
    """Raw multipliers produced by an impact model before formatting.

    ``family`` selects the human-summary phrasing:
    ``standard`` | ``data_loss`` | ``security`` | ``detection``.
    """

    latency_multiplier: float
    cost_multiplier: float
    failure_probability: float
    family: str = "standard"


#: Signature shared by every impact model:
#: ``(row_count, daily_runs, warehouse, base_monthly_cost) -> ModelOutput``.
ImpactModel = Callable[[int, int, str, float], ModelOutput]


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]``; non-finite values collapse to ``lo``."""
    if not math.isfinite(value):
        return lo
    return max(lo, min(value, hi))


def _constant(
    latency: float, cost: float, failure: float, family: str = "standard"
) -> ImpactModel:
    """Build a model that ignores workload parameters and returns fixed factors."""

    def model(
        row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
    ) -> ModelOutput:
        return ModelOutput(latency, cost, failure, family)

    return model


# ---------------------------------------------------------------------------
# Bespoke, parameter-sensitive models
# ---------------------------------------------------------------------------


def _cartesian_join(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """O(n^2) row explosion: latency and cost scale directly with row volume."""
    latency = _clamp(row_count / 1_000, 1.0, 100.0)
    cost = _clamp(row_count / 100_000, 1.0, 50.0)
    failure = 0.35 if row_count > 10_000_000 else 0.10
    return ModelOutput(latency, cost, failure)


def _exploding_join(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Fan-out join (many-to-many key skew) — milder than a pure cross join."""
    latency = _clamp(row_count / 4_000, 1.0, 60.0)
    cost = _clamp(row_count / 400_000, 1.0, 25.0)
    failure = 0.25 if row_count > 10_000_000 else 0.08
    return ModelOutput(latency, cost, failure)


def _unbounded_full_scan(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Full-table scan with no predicate: scan work grows linearly with rows."""
    latency = _clamp(1.0 + row_count / 5_000_000, 1.0, 20.0)
    cost = _clamp(1.0 + row_count / 10_000_000, 1.0, 12.0)
    return ModelOutput(latency, cost, 0.05)


def _missing_partition_filter(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Partition pruning lost. BigQuery bills scanned bytes, so it hurts most."""
    if warehouse == "bigquery":
        latency = _clamp(1.0 + row_count / 2_500_000, 1.0, 30.0)
        cost = _clamp(1.0 + row_count / 1_500_000, 1.0, 25.0)
        failure = 0.08
    else:
        latency = _clamp(1.0 + row_count / 6_000_000, 1.0, 12.0)
        cost = _clamp(1.0 + row_count / 8_000_000, 1.0, 8.0)
        failure = 0.05
    return ModelOutput(latency, cost, failure)


def _nested_loop_risk(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Optimizer likely to pick a nested-loop plan; latency dominates."""
    latency = _clamp(1.0 + row_count / 2_000_000, 1.0, 40.0)
    cost = _clamp(1.0 + row_count / 15_000_000, 1.0, 8.0)
    return ModelOutput(latency, cost, 0.12)


def _correlated_subquery(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Subquery re-executed per outer row: latency scales with row volume."""
    latency = _clamp(1.0 + row_count / 1_000_000, 1.0, 50.0)
    cost = _clamp(1.0 + row_count / 20_000_000, 1.0, 6.0)
    return ModelOutput(latency, cost, 0.08)


def _select_star(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """All columns materialized; on columnar/bytes-billed BigQuery this is pricier."""
    cost = 1.6 if warehouse == "bigquery" else 1.25
    return ModelOutput(1.15, cost, 0.01)


def _window_function_on_full_table(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Unpartitioned window over the whole table: sort spill risk grows with rows."""
    latency = _clamp(1.0 + row_count / 4_000_000, 1.0, 15.0)
    cost = _clamp(1.0 + row_count / 40_000_000, 1.0, 4.0)
    return ModelOutput(latency, cost, 0.10)


def _sensor_poke_mode(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """Poke-mode sensors hold worker slots; starvation grows with run frequency."""
    latency = _clamp(1.0 + daily_runs / 12.0, 1.0, 6.0)
    cost = _clamp(1.0 + daily_runs / 48.0, 1.0, 2.5)
    return ModelOutput(latency, cost, 0.10)


def _missing_watermark(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """No watermark: open windows accumulate forever. Row volume proxies the
    number of in-flight windows, so failure probability scales with it."""
    latency = _clamp(1.0 + row_count / 50_000_000, 1.0, 4.0)
    failure = _clamp(0.10 + row_count / 200_000_000, 0.10, 0.50)
    return ModelOutput(latency, 1.25, failure)


def _unbounded_state(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> ModelOutput:
    """State store grows without TTL/eviction: OOM probability grows with rows."""
    latency = _clamp(1.0 + row_count / 100_000_000, 1.0, 3.0)
    failure = _clamp(0.05 + row_count / 1_000_000_000, 0.05, 0.60)
    return ModelOutput(latency, 1.3, failure)


#: Registry of bespoke impact models keyed by rule id. Rules absent here fall
#: back to the severity x category heuristic in :func:`_fallback_output`.
IMPACT_MODELS: Dict[str, ImpactModel] = {
    # --- SQL / warehouse performance ------------------------------------
    "CARTESIAN_JOIN": _cartesian_join,
    "EXPLODING_JOIN": _exploding_join,
    "UNBOUNDED_FULL_SCAN": _unbounded_full_scan,
    "MISSING_PARTITION_FILTER": _missing_partition_filter,
    "NESTED_LOOP_RISK": _nested_loop_risk,
    "CORRELATED_SUBQUERY": _correlated_subquery,
    "SELECT_STAR": _select_star,
    "WINDOW_FUNCTION_ON_FULL_TABLE": _window_function_on_full_table,
    "UNOPTIMIZED_CTE_REUSE": _constant(1.8, 1.6, 0.03),
    # --- Orchestration reliability --------------------------------------
    "NO_RETRIES": _constant(1.0, 1.0, 0.22),  # baseline transient failure rate
    "NO_CATCHUP_SET": _constant(1.1, 1.4, 0.07),  # surprise backfill storms
    "SENSOR_POKE_MODE": _sensor_poke_mode,
    "XCOMS_LARGE_DATA": _constant(1.35, 1.05, 0.15),  # metadata DB bloat
    "NO_SLA": _constant(1.0, 1.0, 0.05, family="detection"),
    "SEQUENTIAL_HEAVY_TASKS": _constant(2.5, 1.0, 0.04),  # serial-chain proxy
    # --- Streaming / Spark -----------------------------------------------
    "NO_CHECKPOINTING": _constant(1.3, 1.3, 0.30),  # full replay on failure
    "MISSING_WATERMARK": _missing_watermark,
    "UNBOUNDED_STATE": _unbounded_state,
    # --- Data loss / security --------------------------------------------
    "DELETE_WITHOUT_WHERE": _constant(1.0, 2.0, 0.90, family="data_loss"),
    "UPDATE_WITHOUT_WHERE": _constant(1.0, 2.0, 0.90, family="data_loss"),
    "HARDCODED_CREDENTIALS": _constant(1.0, 25.0, 0.02, family="security"),
}


# ---------------------------------------------------------------------------
# Generic fallback: severity x category
# ---------------------------------------------------------------------------

_SEVERITY_BASE: Dict[str, Tuple[float, float, float]] = {
    # severity -> (latency_multiplier, cost_multiplier, failure_probability)
    "CRITICAL": (2.5, 1.8, 0.15),
    "WARNING": (1.4, 1.2, 0.05),
    "INFO": (1.05, 1.02, 0.01),
}
_RELIABILITY_FAILURE: Dict[str, float] = {
    "CRITICAL": 0.35,
    "WARNING": 0.12,
    "INFO": 0.03,
}
#: Categories where the dominant risk is failed runs, not warehouse spend.
_FAILURE_WEIGHTED_CATEGORIES = frozenset({"reliability", "observability"})


def _fallback_output(issue: Issue) -> ModelOutput:
    """Heuristic impact for rules without a bespoke model.

    Performance/cost-ish categories keep the severity baseline; reliability-ish
    categories shift weight from cost onto failure probability.
    """
    latency, cost, failure = _SEVERITY_BASE.get(
        issue.severity, _SEVERITY_BASE["INFO"]
    )
    if issue.category in _FAILURE_WEIGHTED_CATEGORIES:
        failure = _RELIABILITY_FAILURE.get(issue.severity, 0.03)
        cost = 1.0 + (cost - 1.0) * 0.25
        latency = 1.0 + (latency - 1.0) * 0.5
    return ModelOutput(latency, cost, failure)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _sanitize_params(
    row_count: int, daily_runs: int, warehouse: str, base_monthly_cost: float
) -> Tuple[int, int, str, float]:
    """Coerce workload parameters into safe, finite, non-negative values."""
    try:
        rc = max(int(row_count), 0)
    except (TypeError, ValueError):
        rc = 0
    try:
        dr = max(int(daily_runs), 0)
    except (TypeError, ValueError):
        dr = 0
    wh = str(warehouse).strip().lower() if warehouse else ""
    if not wh:
        wh = "snowflake"
    try:
        base = float(base_monthly_cost)
    except (TypeError, ValueError):
        base = 0.0
    if not math.isfinite(base) or base < 0:
        base = 0.0
    return rc, dr, wh, base


def _summarize(
    family: str,
    row_count: int,
    daily_runs: int,
    warehouse: str,
    cost_increase: float,
    incidents: float,
    failure_probability: float,
) -> str:
    """Render the one-line human summary for an impact, phrased per rule family."""
    if family == "data_loss":
        return (
            f"On {row_count:,} rows/run at {daily_runs}x/day, this statement can "
            f"destroy or corrupt production data: ~{failure_probability:.0%} chance "
            f"of business-impacting data loss per run, ~${cost_increase:,.0f}/mo in "
            f"expected {warehouse} recovery and backfill cost, and ~{incidents:.1f} "
            f"incidents/month."
        )
    if family == "security":
        return (
            f"Exposed credentials put this pipeline ({daily_runs}x/day on "
            f"{warehouse}) at breach risk: low per-run probability "
            f"(~{failure_probability:.1%}) but a very high blast radius — "
            f"~${cost_increase:,.0f}/mo in expected exposure and remediation cost "
            f"and ~{incidents:.1f} incidents/month."
        )
    if family == "detection":
        return (
            f"On {row_count:,} rows/run at {daily_runs}x/day, missing SLAs mean "
            f"failures surface late: ~{incidents:.1f} silent incidents/month on "
            f"{warehouse} and ~${cost_increase:,.0f}/mo in delayed-detection cost."
        )
    return (
        f"On {row_count:,} rows/run at {daily_runs}x/day, this issue may increase "
        f"{warehouse} cost by ~${cost_increase:,.0f}/mo and trigger "
        f"~{incidents:.1f} incidents/month."
    )


def _evaluate(
    issue: Issue,
    row_count: int,
    daily_runs: int,
    warehouse: str,
    base_monthly_cost: float,
) -> Tuple[ModelOutput, ImpactResult]:
    """Run the model for ``issue`` and return (clamped factors, ImpactResult)."""
    rc, dr, wh, base = _sanitize_params(
        row_count, daily_runs, warehouse, base_monthly_cost
    )

    model = IMPACT_MODELS.get(issue.rule)
    if model is not None:
        try:
            raw = model(rc, dr, wh, base)
        except Exception:
            logger.exception(
                "Impact model for rule %s failed; using severity fallback", issue.rule
            )
            raw = _fallback_output(issue)
    else:
        logger.debug("No bespoke impact model for rule %s; using fallback", issue.rule)
        raw = _fallback_output(issue)

    clamped = ModelOutput(
        latency_multiplier=_clamp(raw.latency_multiplier, 1.0, _MAX_MULTIPLIER),
        cost_multiplier=_clamp(raw.cost_multiplier, 1.0, _MAX_MULTIPLIER),
        failure_probability=_clamp(
            raw.failure_probability, 0.0, _MAX_FAILURE_PROBABILITY
        ),
        family=raw.family,
    )

    latency_increase_pct = float(round((clamped.latency_multiplier - 1.0) * 100.0))
    monthly_cost_increase = round(
        max(base * (clamped.cost_multiplier - 1.0), 0.0), 2
    )
    failure_per_run = min(
        round(clamped.failure_probability, 3), _MAX_FAILURE_PROBABILITY
    )
    incidents_per_month = round(dr * _DAYS_PER_MONTH * failure_per_run, 1)

    result = ImpactResult(
        issue_id=issue.id,
        rule=issue.rule,
        latency_increase_pct=latency_increase_pct,
        monthly_cost_increase_usd=monthly_cost_increase,
        failure_probability_per_run=failure_per_run,
        pagerduty_incidents_per_month=incidents_per_month,
        human_summary=_summarize(
            clamped.family,
            rc,
            dr,
            wh,
            monthly_cost_increase,
            incidents_per_month,
            failure_per_run,
        ),
    )
    return clamped, result


def simulate_issue_impact(
    issue: Issue,
    *,
    row_count: int,
    daily_runs: int,
    warehouse: str,
    base_monthly_cost: float,
) -> ImpactResult:
    """Simulate the production impact of a single issue.

    Deterministic: the same issue and parameters always produce the same
    result. All numbers are clamped (failure probability capped at 0.95,
    cost increase never negative) and guaranteed finite.

    Args:
        issue: The rule-engine issue to simulate.
        row_count: Rows processed per pipeline run.
        daily_runs: Pipeline executions per day.
        warehouse: Target warehouse (``snowflake``/``bigquery``/``redshift``/
            ``databricks``); some models are warehouse-sensitive.
        base_monthly_cost: Current monthly warehouse spend in USD used as the
            baseline for cost-increase projections.

    Returns:
        A fully populated :class:`~app.schemas.report.ImpactResult`.
    """
    _, result = _evaluate(issue, row_count, daily_runs, warehouse, base_monthly_cost)
    return result


def attach_impacts(
    issues: List[Issue],
    *,
    row_count: int,
    daily_runs: int,
    warehouse: str,
    base_monthly_cost: float,
) -> List[ImpactResult]:
    """Simulate every issue, attach ``issue.impact`` in place, return results.

    Each issue receives an :class:`~app.schemas.report.IssueImpact` with its
    latency percentage, raw cost multiplier and failure probability. The
    returned :class:`~app.schemas.report.ImpactResult` list is sorted by
    ``monthly_cost_increase_usd`` descending (ties broken by failure
    probability, then latency, descending) so the costliest issues lead.
    """
    results: List[ImpactResult] = []
    for issue in issues:
        factors, result = _evaluate(
            issue, row_count, daily_runs, warehouse, base_monthly_cost
        )
        issue.impact = IssueImpact(
            latency_pct=result.latency_increase_pct,
            cost_multiplier=round(factors.cost_multiplier, 3),
            failure_probability=result.failure_probability_per_run,
        )
        results.append(result)

    results.sort(
        key=lambda r: (
            -r.monthly_cost_increase_usd,
            -r.failure_probability_per_run,
            -r.latency_increase_pct,
        )
    )
    return results
