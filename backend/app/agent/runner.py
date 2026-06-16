"""The autonomous Data Pipeline Copilot agent.

:func:`run_agent` drives a single, durable, step-based workflow over one pipeline
and reports both operational KPIs (about the run itself) and business KPIs (the
value the fix would deliver). The workflow is executed by
:class:`app.agent.workflow.WorkflowEngine`, so every step is timed, retried and
recorded as an inspectable :class:`~app.schemas.agent.WorkflowStep`.

Steps
-----
1. ``analyze``        — deterministic full analysis of the input pipeline. A
                        parse error is a HARD failure: the run aborts and the
                        remaining steps are skipped.
2. ``dynamic_review`` — advisory LLM second pass; *skipped* (not failed) when no
                        provider is configured or it surfaces nothing.
3. ``propose_fixes``  — collect the fixable issues (those carrying a
                        ``fix_suggestion`` or ``fix_diff``).
4. ``apply_fixes``    — when enabled AND a provider is available, ask the LLM to
                        rewrite the source applying the top fixable issues.
5. ``re_analyze``     — re-run the deterministic analysis on the fixed code
                        (only when fixed code was produced).
6. ``measure``        — compute the operational + business KPIs and the summary.

The agent ALWAYS produces a deterministic projected "after" state — even with no
LLM in the loop — by re-scoring the pipeline with the fixable issues removed and
reading the cost engine's already-computed optimized monthly cost. When a real
LLM re-analysis exists, its measured numbers are preferred.

:func:`run_agent` NEVER raises: every failure mode degrades to an ``AgentRun``
with ``status="failed"`` and a clear summary.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.agent.workflow import FailurePolicy, SkipStep, StepResult, WorkflowEngine
from app.engines.dynamic import dynamic_review
from app.engines.scoring import compute_score
from app.llm.client import LLMUnavailable, get_provider_status, stream_completion
from app.llm.fix_prompts import build_fix_messages
from app.schemas.agent import AgentKPIs, AgentRun, AgentRunRequest, BusinessKPIs
from app.schemas.ir import ParseError
from app.schemas.report import SEVERITY_ORDER, AnalysisReport, Issue
from app.services.analyzer import analyze_full

logger = logging.getLogger(__name__)

#: How many fixable issues the LLM fix step is asked to resolve at once.
MAX_FIXES_TO_APPLY = 5
#: Per-step retry bound for the (network-bound) LLM steps.
_LLM_MAX_ATTEMPTS = 2

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_fences(text: str) -> str:
    """Remove a single wrapping markdown code fence, if the model added one."""
    if not text:
        return ""
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _sort_by_severity(issues: List[Issue]) -> List[Issue]:
    """Stable re-sort of issues by severity (CRITICAL -> WARNING -> INFO)."""
    return sorted(issues, key=lambda i: SEVERITY_ORDER.get(i.severity, 9))


def _is_fixable(issue: Issue) -> bool:
    """Whether an issue carries an actionable fix the agent can apply."""
    return bool((issue.fix_suggestion or "").strip() or (issue.fix_diff or "").strip())


def _fixable_issues(issues: List[Issue]) -> List[Issue]:
    """The subset of issues that carry a fix suggestion or diff, severity-sorted."""
    return _sort_by_severity([i for i in issues if _is_fixable(i)])


def _count_critical(issues: List[Issue]) -> int:
    return sum(1 for i in issues if i.severity == "CRITICAL")


async def _complete(messages: List[dict]) -> str:
    """Accumulate a streamed LLM completion into a single string."""
    parts: List[str] = []
    async for tok in stream_completion(messages):
        parts.append(tok)
    return "".join(parts)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
async def run_agent(req: AgentRunRequest) -> AgentRun:
    """Run the durable agent workflow over ``req.code`` and report KPIs.

    Returns a fully-populated :class:`AgentRun`. Never raises: a parse error
    yields a graceful ``status="failed"`` run, and any catastrophic error is
    caught and reported in the run's summary.
    """
    created_at = _now_iso()
    try:
        return await _run_agent_inner(req, created_at)
    except Exception:  # absolutely-never-raise guarantee
        logger.exception("Agent run failed catastrophically")
        return AgentRun(
            created_at=created_at,
            status="failed",
            summary=(
                "The agent could not complete this run due to an unexpected "
                "internal error. No changes were made to the pipeline."
            ),
            agent_kpis=AgentKPIs(success=False),
            business_kpis=BusinessKPIs(),
        )


async def _run_agent_inner(req: AgentRunRequest, created_at: str) -> AgentRun:
    """Build and execute the workflow, then assemble the ``AgentRun``."""
    analysis_kwargs = dict(
        format=req.format,
        dialect=req.dialect,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )

    engine = WorkflowEngine(name="data-pipeline-agent")

    # -- Step 1: analyze (HARD failure on parse error -> abort the rest) ------
    async def step_analyze(ctx: Dict[str, Any]) -> StepResult:
        try:
            report, pr = analyze_full(req.code, **analysis_kwargs)
        except ParseError as exc:
            # Surface a clean, human message and abort the workflow.
            line = f" (line {exc.line})" if getattr(exc, "line", None) else ""
            raise RuntimeError(f"Could not parse the pipeline{line}: {exc.message}") from exc
        ctx["report"] = report
        ctx["pr"] = pr
        ctx["initial_issue_count"] = len(report.issues)
        return StepResult(detail=f"Parsed and analyzed: {len(report.issues)} issue(s) found.")

    # -- Step 2: dynamic_review (advisory; skip when nothing/no provider) -----
    async def step_dynamic_review(ctx: Dict[str, Any]) -> StepResult:
        report: AnalysisReport = ctx["report"]
        pr = ctx["pr"]
        dynamic_issues = await dynamic_review(report, pr)
        if not dynamic_issues:
            raise SkipStep("No advisory findings (no LLM provider or nothing novel).")
        report.issues = _sort_by_severity(list(report.issues) + list(dynamic_issues))
        ctx["dynamic_count"] = len(dynamic_issues)
        return StepResult(detail=f"Added {len(dynamic_issues)} advisory finding(s).")

    # -- Step 3: propose_fixes ------------------------------------------------
    async def step_propose_fixes(ctx: Dict[str, Any]) -> StepResult:
        report: AnalysisReport = ctx["report"]
        fixable = _fixable_issues(report.issues)
        ctx["fixable"] = fixable
        ctx["fixes_proposed"] = len(fixable)
        return StepResult(detail=f"Proposed fixes for {len(fixable)} issue(s).")

    # -- Step 4: apply_fixes (LLM; skip when disabled/unavailable) ------------
    async def step_apply_fixes(ctx: Dict[str, Any]) -> StepResult:
        ctx["fixed_code"] = None
        ctx["used_llm"] = False
        if not req.apply_fixes:
            raise SkipStep("Fix application disabled for this run.")
        try:
            available = get_provider_status().available
        except Exception:
            available = False
        if not available:
            raise SkipStep("No LLM provider available to generate fixes.")
        fixable: List[Issue] = ctx.get("fixable", [])
        if not fixable:
            raise SkipStep("No fixable issues to apply.")

        report: AnalysisReport = ctx["report"]
        top = fixable[:MAX_FIXES_TO_APPLY]
        messages = build_fix_messages(
            req.code,
            top,
            pipeline_format=report.format,
            dialect=report.dialect,
        )
        try:
            raw = await _complete(messages)
        except LLMUnavailable as exc:
            raise SkipStep(f"LLM unavailable while generating fixes: {exc}") from exc
        fixed = _strip_fences(raw)
        if not fixed or fixed.strip() == (req.code or "").strip():
            raise SkipStep("LLM did not produce a usable code change.")
        ctx["fixed_code"] = fixed
        ctx["used_llm"] = True
        ctx["fixes_targeted"] = len(top)
        return StepResult(detail=f"Applied {len(top)} fix(es) via LLM.")

    # -- Step 5: re_analyze (only when fixed code exists) ---------------------
    async def step_re_analyze(ctx: Dict[str, Any]) -> StepResult:
        fixed_code = ctx.get("fixed_code")
        if not fixed_code:
            raise SkipStep("No fixed code to re-analyze.")
        try:
            final_report, _ = analyze_full(fixed_code, **analysis_kwargs)
        except ParseError as exc:
            # The LLM produced unparseable code: don't fail the run — fall back
            # to the deterministic projection by leaving final_report unset.
            raise SkipStep(f"Re-analysis skipped: fixed code did not parse ({exc.message}).") from exc
        ctx["final_report"] = final_report
        return StepResult(
            detail=(
                f"Re-analyzed fixed pipeline: "
                f"{len(final_report.issues)} issue(s) remain."
            )
        )

    # -- Step 6: measure (KPIs + summary) -------------------------------------
    async def step_measure(ctx: Dict[str, Any]) -> StepResult:
        business, fixes_applied = _compute_business_kpis(ctx)
        ctx["business_kpis"] = business
        ctx["fixes_applied"] = fixes_applied
        return StepResult(
            detail=(
                f"Score {business.score_before:.0f} -> {business.score_after:.0f}, "
                f"${business.monthly_cost_before:,.0f} -> "
                f"${business.monthly_cost_after:,.0f}/mo."
            )
        )

    (
        engine
        .add_step("analyze", step_analyze, label="Analyze pipeline",
                  on_failure=FailurePolicy.ABORT)
        .add_step("dynamic_review", step_dynamic_review, label="Advisory review",
                  max_attempts=_LLM_MAX_ATTEMPTS)
        .add_step("propose_fixes", step_propose_fixes, label="Propose fixes")
        .add_step("apply_fixes", step_apply_fixes, label="Apply fixes",
                  max_attempts=_LLM_MAX_ATTEMPTS)
        .add_step("re_analyze", step_re_analyze, label="Re-analyze fixed pipeline")
        .add_step("measure", step_measure, label="Measure impact")
    )

    await engine.run()
    return _assemble_run(req, engine, created_at)


# ---------------------------------------------------------------------------
# KPI computation
# ---------------------------------------------------------------------------
def _compute_business_kpis(ctx: Dict[str, Any]) -> Tuple[BusinessKPIs, int]:
    """Compute the BusinessKPIs and the ``fixes_applied`` count.

    Always produces a deterministic projected "after". When a real LLM
    re-analysis exists, its measured score/cost/issue counts are preferred.

    Returns ``(business_kpis, fixes_applied)``.
    """
    report: Optional[AnalysisReport] = ctx.get("report")
    if report is None:
        return BusinessKPIs(), 0

    pr = ctx["pr"]
    fixable: List[Issue] = ctx.get("fixable", [])
    fixable_ids = {id(i) for i in fixable}
    final_report: Optional[AnalysisReport] = ctx.get("final_report")

    # --- BEFORE (always from the initial report) -----------------------------
    issues_before = len(report.issues)
    critical_before = _count_critical(report.issues)
    score_before = float(report.production_score.total)
    monthly_before = float(report.cost_analysis.monthly_usd_current)

    # --- Deterministic projected AFTER (issues minus the fixable set) --------
    remaining_issues = [i for i in report.issues if id(i) not in fixable_ids]
    projected_score = compute_score(remaining_issues, pr)
    projected_score_after = float(projected_score.total)
    projected_monthly_after = float(report.cost_analysis.monthly_usd_optimized)
    projected_resolved = max(0, issues_before - len(remaining_issues))

    if final_report is not None:
        # Real LLM path: prefer the measured numbers.
        score_after = float(final_report.production_score.total)
        monthly_after = float(final_report.cost_analysis.monthly_usd_current)
        issues_after = len(final_report.issues)
        issues_resolved = max(0, issues_before - issues_after)
        critical_after = _count_critical(final_report.issues)
        fixes_applied = issues_resolved
    else:
        # Deterministic projection.
        score_after = projected_score_after
        monthly_after = projected_monthly_after
        issues_after = len(remaining_issues)
        issues_resolved = projected_resolved
        critical_after = _count_critical(remaining_issues)
        fixes_applied = projected_resolved

    # --- Cost deltas ---------------------------------------------------------
    monthly_after = min(monthly_after, monthly_before) if monthly_before else monthly_after
    monthly_savings = max(0.0, monthly_before - monthly_after)
    savings_pct = (
        round(monthly_savings / monthly_before * 100.0, 1) if monthly_before > 0 else 0.0
    )
    annual_savings = round(monthly_savings * 12.0, 2)

    # --- Incidents prevented: sum the fixable issues' per-run failure prob ----
    daily_runs = float(report.params.daily_runs)
    incidents_prevented = 0.0
    for issue in fixable:
        impact = issue.impact
        if impact is not None:
            incidents_prevented += max(0.0, impact.failure_probability) * daily_runs * 30.0
    incidents_prevented = round(max(0.0, incidents_prevented), 2)

    business = BusinessKPIs(
        score_before=round(score_before, 1),
        score_after=round(score_after, 1),
        score_delta=round(score_after - score_before, 1),
        issues_before=issues_before,
        issues_after=issues_after,
        issues_resolved=issues_resolved,
        critical_before=critical_before,
        critical_after=critical_after,
        monthly_cost_before=round(monthly_before, 2),
        monthly_cost_after=round(monthly_after, 2),
        monthly_savings=round(monthly_savings, 2),
        savings_pct=savings_pct,
        annual_savings=annual_savings,
        projected_incidents_prevented=incidents_prevented,
    )
    return business, fixes_applied


def _compute_agent_kpis(
    engine: WorkflowEngine, ctx: Dict[str, Any], *, used_llm: bool
) -> AgentKPIs:
    """Operational KPIs derived from the recorded workflow steps."""
    steps = engine.steps
    succeeded = sum(1 for s in steps if s.status == "success")
    failed = sum(1 for s in steps if s.status == "failed")
    retries = sum(max(0, s.attempts - 1) for s in steps)
    duration_ms = sum(s.duration_ms or 0 for s in steps)

    return AgentKPIs(
        steps_total=len(steps),
        steps_succeeded=succeeded,
        steps_failed=failed,
        retries=retries,
        duration_ms=duration_ms,
        fixes_proposed=int(ctx.get("fixes_proposed", 0)),
        fixes_applied=int(ctx.get("fixes_applied", 0)),
        success=engine.succeeded,
        used_llm=used_llm,
    )


# ---------------------------------------------------------------------------
# Run assembly + summary
# ---------------------------------------------------------------------------
def _assemble_run(
    req: AgentRunRequest, engine: WorkflowEngine, created_at: str
) -> AgentRun:
    """Assemble the final :class:`AgentRun` from the engine trace + context."""
    ctx = engine.context
    report: Optional[AnalysisReport] = ctx.get("report")
    final_report: Optional[AnalysisReport] = ctx.get("final_report")
    fixed_code: Optional[str] = ctx.get("fixed_code")
    used_llm = bool(ctx.get("used_llm", False))
    business: BusinessKPIs = ctx.get("business_kpis") or BusinessKPIs()

    agent_kpis = _compute_agent_kpis(engine, ctx, used_llm=used_llm)

    analyze_record = engine.step("analyze")
    hard_failed = analyze_record is not None and analyze_record.status == "failed"

    if hard_failed:
        status = "failed"
        summary = _failure_summary(analyze_record)
    else:
        status = "completed" if agent_kpis.success else "partial"
        summary = _build_summary(ctx, business, used_llm, n_steps=agent_kpis.steps_total)

    return AgentRun(
        created_at=created_at,
        status=status,
        steps=engine.steps,
        agent_kpis=agent_kpis,
        business_kpis=business,
        initial_report=report,
        final_report=final_report,
        fixed_code=fixed_code,
        summary=summary,
    )


def _failure_summary(analyze_record: Any) -> str:
    """Summary for a run that hard-failed at the analyze step."""
    detail = (getattr(analyze_record, "detail", "") or "the pipeline could not be parsed").rstrip(".")
    return (
        f"The agent stopped at the analysis step: {detail}. "
        "No fixes were proposed or applied."
    )


def _build_summary(
    ctx: Dict[str, Any], business: BusinessKPIs, used_llm: bool, *, n_steps: int
) -> str:
    """Build a 2-3 sentence deterministic summary of the run."""
    total_issues = business.issues_before
    advisory = int(ctx.get("dynamic_count", 0))

    first = (
        f"The agent ran {n_steps} steps and surfaced "
        f"{total_issues} issue{'' if total_issues == 1 else 's'}"
    )
    if advisory:
        first += f" ({advisory} advisory)"
    first += "."

    mode = "applied fixes and re-measured" if (used_llm and ctx.get("final_report")) else "projects the fixes would"
    second = (
        f"It {mode} lift the readiness score from "
        f"{business.score_before:.0f} to {business.score_after:.0f} and cut cost "
        f"from ${business.monthly_cost_before:,.0f} to "
        f"${business.monthly_cost_after:,.0f}/mo "
        f"(~${business.annual_savings:,.0f}/yr)."
    )

    third = ""
    if business.projected_incidents_prevented >= 1:
        third = (
            f" That is an estimated {business.projected_incidents_prevented:,.0f} "
            "pipeline incident(s) prevented per month."
        )
    return f"{first} {second}{third}"
