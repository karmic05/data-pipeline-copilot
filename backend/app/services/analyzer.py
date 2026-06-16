"""Full deterministic analysis orchestration.

``analyze_full`` runs the entire pipeline in contract order:

    parse -> run_rules -> estimate_cost -> attach_impacts -> build_lineage
    -> compute_score -> generate_tests -> scan_security
    -> deterministic summary + optimizations -> AnalysisReport

Everything here is deterministic - the LLM layer is never invoked. Engine
failures are isolated: a crashing engine logs the exception and degrades to
that section's schema defaults instead of losing the whole analysis.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

from app.engines.cost import estimate_cost
from app.engines.impact import attach_impacts
from app.engines.lineage import build_lineage
from app.engines.observability import generate_tests
from app.engines.scoring import compute_score
from app.engines.security import scan_security
from app.parsers import parse
from app.rules import run_rules
from app.schemas.ir import IR, ParseResult
from app.schemas.report import (
    SEVERITY_ORDER,
    AnalysisParams,
    AnalysisReport,
    CostAnalysis,
    GeneratedTests,
    ImpactResult,
    Issue,
    LineageGraph,
    ProductionScore,
    SecurityReport,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Human-friendly names for each pipeline format, used in the summary.
_FORMAT_LABELS: Dict[str, str] = {
    "sql": "SQL",
    "airflow": "Airflow DAG",
    "dbt": "dbt model",
    "spark": "PySpark",
    "prefect": "Prefect/Dagster flow",
    "flink": "Flink SQL",
    "kafka": "Kafka Streams",
    "great_expectations": "Great Expectations suite",
}

#: Cap on the deterministic optimization suggestions surfaced on the report.
_MAX_OPTIMIZATIONS = 5
#: Max table names listed verbatim in the summary before collapsing to "+N more".
_MAX_NAMED_TABLES = 4


def _safe(stage: str, fn: Callable[[], T], fallback: T) -> T:
    """Run one engine stage; on failure log and return ``fallback``."""
    try:
        return fn()
    except Exception:
        logger.exception("Engine stage %r failed; falling back to defaults", stage)
        return fallback


def _plural(count: int, noun: str) -> str:
    """Format ``count noun`` with naive English pluralization."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _join_names(names: List[str]) -> str:
    """Join table names into a readable list, collapsing long lists."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= _MAX_NAMED_TABLES:
        return ", ".join(names[:-1]) + " and " + names[-1]
    shown = ", ".join(names[:_MAX_NAMED_TABLES])
    return f"{shown} and {len(names) - _MAX_NAMED_TABLES} more"


def _build_summary(ir: IR) -> str:
    """Build the deterministic 1-3 sentence report summary from the IR alone.

    Names the format, dialect, read tables -> write target, and the operation
    mix (join / aggregation / window counts). No LLM involvement.
    """
    label = _FORMAT_LABELS.get(ir.format, ir.format)
    dialect_part = f" ({ir.dialect} dialect)" if ir.dialect else ""

    reads = sorted({t.name for t in ir.tables_by_access("read")})
    writes = sorted({t.name for t in ir.tables_by_access("write")})
    if reads and writes:
        flow = f"reading {_join_names(reads)} and writing to {_join_names(writes)}"
    elif reads:
        flow = f"reading {_join_names(reads)} with no explicit write target"
    elif writes:
        flow = f"writing to {_join_names(writes)}"
    else:
        flow = "with no table references detected"
    first = f"{label} pipeline{dialect_part} {flow}."

    joins = len(ir.ops("JOIN"))
    aggregations = len(ir.ops("AGGREGATE", "GROUP_BY"))
    windows = len(ir.ops("WINDOW"))
    second = (
        f"Operation mix: {_plural(joins, 'join')}, "
        f"{_plural(aggregations, 'aggregation')} and "
        f"{_plural(windows, 'window function')} across "
        f"{_plural(len(ir.operations), 'parsed operation')}."
    )
    return f"{first} {second}"


def _impact_score(issue: Issue) -> float:
    """Scalar impact magnitude for ranking (higher means worse)."""
    impact = issue.impact
    if impact is None:
        return 0.0
    return (
        max(impact.cost_multiplier - 1.0, 0.0)
        + max(impact.latency_pct, 0.0) / 100.0
        + max(impact.failure_probability, 0.0)
    )


def _build_optimizations(issues: List[Issue]) -> List[str]:
    """Up to 5 optimization strings from the highest-impact issues.

    Each entry combines the rule title with its fix suggestion (falling back
    to the issue message when a rule provides no fix). One entry per rule id,
    ranked by simulated impact then severity for a stable, deterministic order.
    """
    ranked = sorted(
        issues,
        key=lambda i: (-_impact_score(i), SEVERITY_ORDER.get(i.severity, 9), i.rule),
    )
    optimizations: List[str] = []
    seen_rules: set[str] = set()
    for issue in ranked:
        if issue.rule in seen_rules:
            continue
        seen_rules.add(issue.rule)
        detail = issue.fix_suggestion or issue.message
        optimizations.append(f"{issue.title}: {detail}" if detail else issue.title)
        if len(optimizations) >= _MAX_OPTIMIZATIONS:
            break
    return optimizations


def analyze_full(
    code: str,
    *,
    format: str = "auto",
    dialect: Optional[str] = None,
    row_count: int = 10_000_000,
    daily_runs: int = 24,
    warehouse: str = "snowflake",
) -> Tuple[AnalysisReport, ParseResult]:
    """Run the full deterministic analysis pipeline over raw source code.

    Returns the assembled :class:`AnalysisReport` together with the
    :class:`ParseResult` so callers can store both (the simulate/cost
    endpoints re-run the cost and impact engines against the parse result).

    Raises :class:`app.schemas.ir.ParseError` when the input cannot be parsed.
    """
    pr = parse(code, format=format, dialect=dialect)
    issues = run_rules(pr)

    cost = _safe(
        "cost",
        lambda: estimate_cost(
            pr, issues, row_count=row_count, daily_runs=daily_runs, warehouse=warehouse
        ),
        CostAnalysis(),
    )
    impacts: List[ImpactResult] = _safe(
        "impact",
        lambda: attach_impacts(
            issues,
            row_count=row_count,
            daily_runs=daily_runs,
            warehouse=warehouse,
            base_monthly_cost=cost.monthly_usd_current,
        ),
        [],
    )
    lineage = _safe("lineage", lambda: build_lineage(pr.ir), LineageGraph())
    score = _safe("scoring", lambda: compute_score(issues, pr), ProductionScore())
    tests = _safe("observability", lambda: generate_tests(pr, issues), GeneratedTests())
    security = _safe("security", lambda: scan_security(pr), SecurityReport())

    report = AnalysisReport(
        created_at=datetime.now(timezone.utc).isoformat(),
        format=pr.ir.format,
        dialect=pr.ir.dialect,
        summary=_build_summary(pr.ir),
        issues=issues,
        optimizations=_build_optimizations(issues),
        cost_analysis=cost,
        lineage=lineage,
        production_score=score,
        impacts=impacts,
        generated_tests=tests,
        security=security,
        params=AnalysisParams(
            row_count=row_count, daily_runs=daily_runs, warehouse=warehouse
        ),
        parser_warnings=pr.warnings,
        ir=pr.ir,
    )
    logger.info(
        "Analyzed %s pipeline %s: %d issues, score %.0f",
        report.format,
        report.id,
        len(issues),
        report.production_score.total,
    )
    return report, pr


def analyze(code: str, **kwargs) -> AnalysisReport:
    """Convenience wrapper over :func:`analyze_full` returning just the report."""
    report, _ = analyze_full(code, **kwargs)
    return report
