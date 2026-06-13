"""LLM task orchestration with deterministic offline fallbacks.

``stream_task`` first tries the configured live provider via
:func:`app.llm.client.stream_completion`. If no provider is available
(:class:`~app.llm.client.LLMUnavailable`), it streams a genuinely useful
template-based narrative built from the deterministic
:class:`~app.schemas.report.AnalysisReport`, so the product works with zero
LLM providers configured. Fallback text is yielded in small word chunks with
a short delay so the UI visibly streams either way.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator, Dict, List, Optional

from app.llm.client import LLMUnavailable, get_provider_status, stream_completion
from app.llm.prompts import TASKS, build_task_messages
from app.schemas.report import AnalysisReport, Issue

logger = logging.getLogger(__name__)

#: Prefix prepended to every deterministic fallback narrative.
OFFLINE_PREFIX = (
    "[offline analysis — start Ollama or set an API key for live LLM "
    "reasoning]\n\n"
)

_CHUNK_WORDS = 8
_CHUNK_DELAY_S = 0.02
_TOKEN_RE = re.compile(r"\S+\s*")

#: Business-impact framing per issue category, used by the offline narrative.
_CATEGORY_IMPACT: Dict[str, str] = {
    "performance": (
        "slow runs push back every downstream dashboard and SLA, and the "
        "extra compute time is billed on every single run."
    ),
    "reliability": (
        "intermittent failures mean stale or missing data for the business, "
        "plus on-call pages and manual backfills that eat engineering time."
    ),
    "observability": (
        "without tests and alerts, bad data reaches stakeholders silently — "
        "you find out from an angry email instead of a failed check."
    ),
    "maintainability": (
        "hard-to-change pipelines slow every future feature and raise the "
        "odds that the next edit introduces a regression."
    ),
    "security": (
        "exposed credentials or unprotected PII create compliance liability "
        "(GDPR/CCPA) and breach risk that dwarfs any compute bill."
    ),
    "cost": (
        "this directly inflates the warehouse bill month after month until "
        "it is fixed."
    ),
}


def _usd(value: float) -> str:
    """Format a dollar amount compactly for narrative text."""
    if abs(value) >= 100:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def _severity_counts(issues: List[Issue]) -> Dict[str, int]:
    counts = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts


def _cost_by_issue(report: AnalysisReport) -> Dict[str, float]:
    """Map issue id -> simulated monthly cost increase (USD)."""
    return {imp.issue_id: imp.monthly_cost_increase_usd for imp in report.impacts}


def _resolve_issue(report: AnalysisReport, issue_id: Optional[str]) -> Optional[Issue]:
    """Find the issue with ``issue_id``, falling back gracefully.

    If the id is missing or unknown, the most severe issue (issues are
    severity-sorted by the rule engine) is used instead; ``None`` only when
    the report has no issues at all.
    """
    if issue_id:
        for issue in report.issues:
            if issue.id == issue_id:
                return issue
        logger.warning(
            "issue_id %r not found in report %s; falling back to top issue",
            issue_id,
            report.id,
        )
    return report.issues[0] if report.issues else None


# ---------------------------------------------------------------------------
# Deterministic fallback narratives (one builder per task)
# ---------------------------------------------------------------------------


def _fallback_explain(report: AnalysisReport, issue: Optional[Issue]) -> str:
    fmt = report.format + (f" ({report.dialect})" if report.dialect else "")
    lines: List[str] = [f"Pipeline overview — detected format: {fmt}."]
    if report.summary:
        lines.append(report.summary)

    labels = {node.id: node.label for node in report.lineage.nodes}
    edges = report.lineage.edges[:12]
    if edges:
        lines.append("")
        lines.append("Data flow:")
        for edge in edges:
            src = labels.get(edge.source, edge.source)
            dst = labels.get(edge.target, edge.target)
            lines.append(f"- {src} -> {dst} ({edge.transformation})")
        if len(report.lineage.edges) > len(edges):
            lines.append(f"- … and {len(report.lineage.edges) - len(edges)} more edges.")
    elif labels:
        names = ", ".join(list(labels.values())[:12])
        lines.append("")
        lines.append(f"Datasets involved: {names}.")

    counts = _severity_counts(report.issues)
    score = report.production_score
    lines.append("")
    lines.append(
        f"The analyzer found {len(report.issues)} issue(s): "
        f"{counts['CRITICAL']} critical, {counts['WARNING']} warning, "
        f"{counts['INFO']} info. Production readiness score: "
        f"{score.total:.0f}/100 (efficiency {score.efficiency:.0f}, "
        f"reliability {score.reliability:.0f}, observability "
        f"{score.observability:.0f}, maintainability "
        f"{score.maintainability:.0f}, security {score.security:.0f})."
    )
    if issue is None and report.issues:
        top = report.issues[0]
        lines.append(
            f"Start with the most severe finding: {top.title} ({top.rule})."
        )
    return "\n".join(lines)


def _fallback_issue(report: AnalysisReport, issue: Optional[Issue]) -> str:
    if issue is None:
        if not report.issues:
            return (
                "No issues were detected in this pipeline, so there is "
                "nothing to explain — the rule engine gave it a clean bill "
                "of health."
            )
        lines = ["The requested issue could not be found. Top detected issues:"]
        for item in report.issues[:5]:
            lines.append(f"- [{item.severity}] {item.title} ({item.rule})")
        return "\n".join(lines)

    lines = [f"{issue.title} — {issue.severity} {issue.category} issue ({issue.rule})."]
    if issue.location and issue.location.line:
        lines.append(f"Flagged at line {issue.location.line}.")
    lines.append("")
    lines.append(issue.message)
    lines.append("")
    impact_text = _CATEGORY_IMPACT.get(
        issue.category, "it erodes trust in the data the business relies on."
    )
    lines.append(f"Why it matters: {impact_text}")
    if issue.impact is not None:
        lines.append(
            f"Simulated impact: ~{issue.impact.latency_pct:.0f}% extra latency, "
            f"{issue.impact.cost_multiplier:.2f}x cost multiplier, "
            f"{issue.impact.failure_probability * 100:.0f}% failure probability "
            "per run."
        )
    if issue.fix_suggestion:
        lines.append("")
        lines.append(f"Recommended fix: {issue.fix_suggestion}")
    if issue.fix_diff:
        lines.append("")
        lines.append("Proposed change:")
        lines.append(issue.fix_diff)
    return "\n".join(lines)


def _fallback_optimize(report: AnalysisReport, issue: Optional[Issue]) -> str:
    if not report.issues:
        return (
            "No issues were detected, so there are no optimizations to "
            "prioritize — this pipeline already looks production-clean."
        )
    costs = _cost_by_issue(report)
    ranked = sorted(report.issues, key=lambda i: costs.get(i.id, 0.0), reverse=True)
    top = ranked[:3]

    lines = ["Optimization plan — top opportunities ranked by monthly cost impact:"]
    for rank, item in enumerate(top, start=1):
        cost = costs.get(item.id, 0.0)
        cost_note = (
            f"adds ~{_usd(cost)}/month at current volume"
            if cost > 0
            else f"{item.severity.lower()} {item.category} risk"
        )
        lines.append("")
        lines.append(f"{rank}. {item.title} ({item.rule}) — {cost_note}.")
        if item.fix_suggestion:
            lines.append(f"   Fix: {item.fix_suggestion}")
        if item.fix_diff:
            lines.append(f"   Change:\n{item.fix_diff}")

    ca = report.cost_analysis
    if ca.monthly_usd_current > 0:
        lines.append("")
        lines.append(
            f"Applying the recommended fixes is estimated to take the bill "
            f"from {_usd(ca.monthly_usd_current)}/month to "
            f"{_usd(ca.monthly_usd_optimized)}/month "
            f"({ca.savings_pct:.0f}% savings)."
        )
    return "\n".join(lines)


def _fallback_cost(report: AnalysisReport, issue: Optional[Issue]) -> str:
    ca = report.cost_analysis
    lines = [
        f"Cost analysis ({ca.provider}, {report.params.row_count:,} rows, "
        f"{report.params.daily_runs} runs/day): roughly "
        f"{_usd(ca.monthly_usd_current)}/month as written vs "
        f"{_usd(ca.monthly_usd_optimized)}/month after the recommended fixes "
        f"({ca.savings_pct:.0f}% savings, cost risk level {ca.risk_level}).",
        "",
        f"Projections: 90 days {_usd(ca.projections.d90_current)} current vs "
        f"{_usd(ca.projections.d90_optimized)} optimized; one year "
        f"{_usd(ca.projections.d365_current)} vs "
        f"{_usd(ca.projections.d365_optimized)}.",
    ]
    if ca.reasoning:
        lines.append("")
        lines.append("How the estimate was built:")
        for reason in ca.reasoning[:8]:
            lines.append(f"- {reason}")

    costs = _cost_by_issue(report)
    if costs:
        best_id = max(costs, key=lambda k: costs[k])
        best_issue = next((i for i in report.issues if i.id == best_id), None)
        if best_issue is not None and costs[best_id] > 0:
            lines.append("")
            lines.append(
                f"Highest-ROI fix: {best_issue.title} ({best_issue.rule}) — "
                f"worth ~{_usd(costs[best_id])}/month."
            )
            if best_issue.fix_suggestion:
                lines.append(f"Fix: {best_issue.fix_suggestion}")
    return "\n".join(lines)


def _fallback_observability(report: AnalysisReport, issue: Optional[Issue]) -> str:
    gt = report.generated_tests
    lines = [
        "Observability assessment — score "
        f"{report.production_score.observability:.0f}/100."
    ]
    if gt.coverage_gaps:
        lines.append("")
        lines.append("Coverage gaps detected:")
        for gap in gt.coverage_gaps[:10]:
            lines.append(f"- {gap}")
        lines.append("")
        lines.append(
            "Each gap is a failure mode that would reach stakeholders "
            "silently — bad data ships without a single check firing."
        )
    else:
        lines.append("")
        lines.append("No major test-coverage gaps were detected.")

    if gt.tests:
        sample = ", ".join(f"{t.name} on {t.target}" for t in gt.tests[:4])
        lines.append("")
        lines.append(
            f"A ready-to-apply test suite was generated ({len(gt.tests)} "
            "tests as dbt schema YAML and a Great Expectations suite) — "
            f"for example: {sample}. Copy it from the Observability tab to "
            "close the gaps before the next deploy."
        )
    else:
        lines.append("")
        lines.append(
            "See the Observability tab for the generated dbt schema tests "
            "and Great Expectations suite as a starting point."
        )
    return "\n".join(lines)


_FALLBACK_BUILDERS = {
    "explain": _fallback_explain,
    "issue": _fallback_issue,
    "optimize": _fallback_optimize,
    "cost": _fallback_cost,
    "observability": _fallback_observability,
}


def _build_fallback(task: str, report: AnalysisReport, issue: Optional[Issue]) -> str:
    """Deterministic, template-based narrative for one task."""
    builder = _FALLBACK_BUILDERS.get(task, _fallback_explain)
    try:
        return builder(report, issue)
    except Exception:  # defensive: a fallback must never crash the stream
        logger.exception("Fallback builder for task %r crashed", task)
        return report.summary or "Analysis complete — see the report tabs for details."


async def _stream_text(text: str) -> AsyncIterator[str]:
    """Yield ``text`` in small word chunks so the UI visibly streams."""
    tokens = _TOKEN_RE.findall(text)
    for start in range(0, len(tokens), _CHUNK_WORDS):
        yield "".join(tokens[start : start + _CHUNK_WORDS])
        await asyncio.sleep(_CHUNK_DELAY_S)


async def stream_task(
    task: str,
    report: AnalysisReport,
    issue_id: Optional[str] = None,
) -> AsyncIterator[str]:
    """Stream LLM reasoning for ``task`` over an analysis report.

    Tries the configured live provider first; if it is unavailable, streams
    a deterministic fallback narrative built from the report so the product
    works with zero providers configured. For task ``"issue"`` the issue is
    resolved by ``issue_id`` (falling back to the most severe issue when the
    id is missing or unknown).
    """
    if task not in TASKS:
        logger.warning("Unknown LLM task %r; defaulting to 'explain'", task)
        task = "explain"
    issue = _resolve_issue(report, issue_id) if task == "issue" else None

    # Only attempt the live provider when it is known-available. The status
    # probe is fast (cached ~30s, 2s timeout) and runs off the event loop;
    # skipping the live call when the provider is down avoids stalling the
    # whole stream for the client's connection timeout before falling back.
    provider_available = False
    try:
        status = await asyncio.to_thread(get_provider_status)
        provider_available = status.available
    except Exception:  # a status probe failure must not block the fallback
        logger.debug("Provider status probe failed; using offline fallback")

    streamed_any = False
    if provider_available:
        try:
            messages = build_task_messages(task, report, issue)
            async for token in stream_completion(messages):
                streamed_any = True
                yield token
            return
        except LLMUnavailable as exc:
            logger.info("LLM provider unavailable (%s); using offline fallback", exc)
            if streamed_any:
                # The live stream died mid-response; appending a full template
                # would garble the partial answer, so close out cleanly instead.
                yield f"\n\n[live stream interrupted: {exc}]"
                return

    fallback = OFFLINE_PREFIX + _build_fallback(task, report, issue)
    async for chunk in _stream_text(fallback):
        yield chunk
