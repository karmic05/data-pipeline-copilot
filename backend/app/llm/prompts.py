"""Prompt construction for the LLM reasoning layer.

The LLM never sees raw pipeline source code. ``build_task_messages`` compacts
the deterministic :class:`~app.schemas.report.AnalysisReport` into a small
structured-JSON context (hard-capped at roughly 8000 characters) and pairs it
with a per-task instruction from :data:`TASKS` under the shared
:data:`SYSTEM_PROMPT`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from app.schemas.report import AnalysisReport, Issue

logger = logging.getLogger(__name__)

#: Approximate hard cap (characters) for the serialized report JSON embedded
#: in the user message. Lists are truncated progressively to fit.
PROMPT_JSON_CHAR_CAP = 8000

SYSTEM_PROMPT: str = (
    "You are a senior data platform engineer reviewing the results of an "
    "automated pipeline analysis. You receive ONLY structured analysis JSON "
    "(detected issues, cost estimates, lineage, scores) - never the raw "
    "source code - so reason strictly from the fields present in the "
    "provided JSON.\n"
    "\n"
    "Hard rules:\n"
    "- Reference only tables, columns, rules, and numbers that appear in the "
    "provided JSON. Never invent table names, column names, or metrics.\n"
    "- When you suggest a code change, present it as a unified diff "
    "(`--- current` / `+++ optimized` with `-`/`+` lines), building on any "
    "fix_diff already provided.\n"
    "- Keep each issue discussion under 150 words.\n"
    "- Always frame findings in terms of business impact: money, latency, "
    "missed SLAs, on-call burden, or compliance risk - not just technical "
    "correctness.\n"
    "- Be direct and practical, like a code-review comment from a trusted "
    "staff engineer. If the data does not support a claim, say so instead "
    "of guessing."
)

#: Per-task instructions. Keys are the task names accepted by the API:
#: explain | issue | optimize | cost | observability.
TASKS: Dict[str, str] = {
    "explain": (
        "Explain what this pipeline does end to end in plain language. Walk "
        "through the data flow using the summary and lineage node names "
        "(sources, transformations, outputs), then call out the most "
        "important risks from the issue list and what they mean for the "
        "business. Close with a one-line verdict on production readiness "
        "based on the score dimensions."
    ),
    "issue": (
        "Explain the single issue embedded in the JSON under 'selected_issue'. "
        "Cover: what is wrong, why it matters to the business (cost, latency, "
        "reliability, or compliance), and exactly how to fix it. Present the "
        "fix as a unified diff, refining the provided fix_diff if one exists. "
        "Stay under 150 words."
    ),
    "optimize": (
        "Produce a prioritized optimization plan from the issue list. Rank "
        "the changes by expected cost and latency payoff using the cost "
        "numbers and issue severities provided, give a concrete fix for each "
        "(unified diff where a fix_diff exists), and estimate the combined "
        "monthly savings using only the numbers in the JSON."
    ),
    "cost": (
        "Explain the cost analysis: where the money goes (use the reasoning "
        "lines), how the current monthly figure compares to the optimized "
        "one, what the 30/90/365-day projections imply, and which single fix "
        "has the highest return on effort. Quote only the dollar figures "
        "present in the JSON."
    ),
    "observability": (
        "Assess the observability of this pipeline. Discuss each coverage "
        "gap listed, what failure mode it would hide in production, and "
        "which tests or alerts to add first. Reference the generated test "
        "suite (dbt schema tests and Great Expectations suite) as the "
        "starting point and explain the on-call impact of leaving the gaps "
        "open."
    ),
}


def _truncate_text(value: Optional[str], limit: int) -> Optional[str]:
    """Trim ``value`` to ``limit`` characters with an ellipsis marker."""
    if value is None or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _compact_issue(issue: Issue) -> Dict[str, Any]:
    """Slim, list-friendly view of one issue for the prompt context."""
    return {
        "rule": issue.rule,
        "severity": issue.severity,
        "title": issue.title,
        "message": issue.message,
        "line": issue.location.line if issue.location else None,
    }


def _compact_report(report: AnalysisReport, issue: Optional[Issue]) -> Dict[str, Any]:
    """Build the compact JSON-serializable context sent to the LLM.

    Includes format, dialect, summary, score dimensions, cost numbers with
    reasoning, up to 15 issues, lineage node names and analysis params -
    never raw source code or the full IR.
    """
    score = report.production_score
    cost = report.cost_analysis
    payload: Dict[str, Any] = {
        "format": report.format,
        "dialect": report.dialect,
        "summary": report.summary,
        "production_score": {
            "total": score.total,
            "efficiency": score.efficiency,
            "reliability": score.reliability,
            "observability": score.observability,
            "maintainability": score.maintainability,
            "security": score.security,
        },
        "cost": {
            "provider": cost.provider,
            "credits_per_run": cost.credits_per_run,
            "monthly_usd_current": cost.monthly_usd_current,
            "monthly_usd_optimized": cost.monthly_usd_optimized,
            "savings_pct": cost.savings_pct,
            "risk_level": cost.risk_level,
            "projections": cost.projections.model_dump(mode="json"),
            "reasoning": list(cost.reasoning),
        },
        "issue_count": len(report.issues),
        "issues": [_compact_issue(i) for i in report.issues[:15]],
        "lineage_node_names": [node.label for node in report.lineage.nodes],
        "params": report.params.model_dump(mode="json"),
    }
    if report.generated_tests.coverage_gaps:
        payload["coverage_gaps"] = list(report.generated_tests.coverage_gaps)
    if issue is not None:
        payload["selected_issue"] = issue.model_dump(mode="json")
    return payload


def _shrink_steps() -> List[Callable[[Dict[str, Any]], None]]:
    """Ordered, increasingly aggressive list-truncation steps for the cap."""

    def issues_to(n: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            p["issues"] = p.get("issues", [])[:n]

        return step

    def lineage_to(n: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            p["lineage_node_names"] = p.get("lineage_node_names", [])[:n]

        return step

    def reasoning_to(n: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            p["cost"]["reasoning"] = p["cost"].get("reasoning", [])[:n]

        return step

    def gaps_to(n: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            if "coverage_gaps" in p:
                p["coverage_gaps"] = p["coverage_gaps"][:n]

        return step

    def trim_messages(limit: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            for item in p.get("issues", []):
                item["message"] = _truncate_text(item.get("message"), limit)

        return step

    def trim_selected(diff_limit: int, msg_limit: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            selected = p.get("selected_issue")
            if isinstance(selected, dict):
                selected["fix_diff"] = _truncate_text(selected.get("fix_diff"), diff_limit)
                selected["message"] = _truncate_text(selected.get("message"), msg_limit)

        return step

    def trim_summary(limit: int) -> Callable[[Dict[str, Any]], None]:
        def step(p: Dict[str, Any]) -> None:
            p["summary"] = _truncate_text(p.get("summary"), limit) or ""

        return step

    return [
        lineage_to(25),
        issues_to(10),
        trim_messages(200),
        reasoning_to(6),
        gaps_to(6),
        lineage_to(12),
        issues_to(6),
        trim_selected(2000, 400),
        trim_summary(400),
        issues_to(3),
        lineage_to(5),
        reasoning_to(3),
        trim_selected(600, 200),
        issues_to(1),
    ]


def _serialize_capped(payload: Dict[str, Any], cap: int = PROMPT_JSON_CHAR_CAP) -> str:
    """Serialize ``payload`` to JSON, truncating lists until it fits ``cap``."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= cap:
        return text
    for step in _shrink_steps():
        step(payload)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) <= cap:
            return text
    # Pathological input (e.g. an enormous single string): hard-slice as the
    # last resort - the model tolerates a truncated trailing field.
    logger.warning(
        "Prompt context still %d chars after truncation; hard-slicing to %d",
        len(text),
        cap,
    )
    return text[:cap]


def build_task_messages(
    task: str,
    report: AnalysisReport,
    issue: Optional[Issue],
) -> List[dict]:
    """Build the ``[system, user]`` chat messages for one reasoning task.

    ``task`` must be a key of :data:`TASKS`. For task ``"issue"`` the full
    ``issue`` JSON is embedded under ``selected_issue``; for other tasks the
    ``issue`` argument is ignored. Raises :class:`ValueError` for unknown
    tasks.
    """
    if task not in TASKS:
        raise ValueError(
            f"Unknown LLM task {task!r}; expected one of {sorted(TASKS)}"
        )
    selected = issue if task == "issue" else None
    payload = _compact_report(report, selected)
    context_json = _serialize_capped(payload)

    instruction = TASKS[task]
    if task == "issue" and selected is None:
        instruction += (
            " No 'selected_issue' could be resolved for this request, so "
            "address the most severe issue in the issues list instead."
        )

    user_content = (
        f"{instruction}\n\n"
        "Pipeline analysis context (structured JSON produced by the "
        "deterministic analyzer - no source code is available):\n"
        f"```json\n{context_json}\n```"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
