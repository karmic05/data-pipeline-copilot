"""Prompt construction for the LLM FIX-generation step of the agent.

The agent's ``apply_fixes`` step asks a configured LLM to rewrite a pipeline so
the highest-impact issues found by the deterministic engine (and the advisory
dynamic review) are resolved. Unlike the dynamic-review and generator prompts,
this step DOES receive the raw pipeline source - it must edit the actual code -
but it only ever returns the corrected code, never prose.

``build_fix_messages`` pairs a senior-engineer system prompt with a per-request
instruction that lists the issues to fix (rule id, title, message and the
engine's own ``fix_suggestion``) and embeds the current source. The model is
told to output ONLY the full corrected pipeline, no commentary and no markdown
fences - :func:`app.agent.runner._strip_fences` defensively strips any fences
the model adds anyway.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from app.schemas.report import Issue

logger = logging.getLogger(__name__)

#: Hard cap (characters) on the embedded source so a pathologically large
#: pipeline cannot blow the context window. The source is sliced, never the
#: instructions, so the model always sees the full task framing.
SOURCE_CHAR_CAP = 16000

#: Hard cap on how many issues we describe in the prompt. The runner already
#: passes only the top handful of fixable issues, but we defend the bound here.
MAX_ISSUES_IN_PROMPT = 8

FIX_SYSTEM_PROMPT: str = (
    "You are a senior data engineer performing an automated code fix. You are "
    "given a data pipeline (SQL, dbt, Airflow, PySpark, or Flink) together with "
    "a list of concrete, verified issues found by a static analyzer, each with a "
    "recommended remediation.\n"
    "\n"
    "Your job: rewrite the pipeline so that EVERY listed issue is resolved, "
    "while preserving the pipeline's original intent, table/column names, "
    "dialect and output schema. Apply the recommended fixes faithfully; do not "
    "introduce unrelated changes, invent new business logic, or drop columns "
    "the pipeline already produces (except where a fix explicitly narrows an "
    "over-broad SELECT *).\n"
    "\n"
    "Hard rules:\n"
    "- Keep the same pipeline format and SQL dialect as the input.\n"
    "- Make the smallest set of edits that fully resolves the listed issues.\n"
    "- The result MUST be syntactically valid, runnable code.\n"
    "- Output ONLY the complete corrected pipeline code. No prose, no "
    "explanation, no diff, and NO surrounding markdown code fences - just the "
    "raw code, ready to save to a file and run."
)


def _format_issue(index: int, issue: Issue) -> str:
    """Render one issue as a numbered, model-friendly instruction block."""
    parts: List[str] = [
        f"{index}. [{issue.severity}/{issue.category}] {issue.title} "
        f"(rule: {issue.rule})"
    ]
    location = getattr(issue, "location", None)
    if location is not None and getattr(location, "line", 0):
        parts[0] += f" - around line {location.line}"
    message = (issue.message or "").strip()
    if message:
        parts.append(f"   Problem: {message}")
    suggestion = (issue.fix_suggestion or "").strip()
    if suggestion:
        parts.append(f"   Recommended fix: {suggestion}")
    diff = (issue.fix_diff or "").strip()
    if diff:
        parts.append(f"   Reference diff:\n{diff}")
    return "\n".join(parts)


def _format_issues(issues: List[Issue]) -> str:
    """Render the issue list (capped) as a numbered remediation checklist."""
    selected = issues[:MAX_ISSUES_IN_PROMPT]
    blocks = [_format_issue(i + 1, issue) for i, issue in enumerate(selected)]
    return "\n".join(blocks)


def _cap_source(code: str, cap: int = SOURCE_CHAR_CAP) -> str:
    """Slice the source to ``cap`` chars (rare; keeps the request bounded)."""
    code = code or ""
    if len(code) <= cap:
        return code
    logger.debug("Fix prompt source %d chars; slicing to %d", len(code), cap)
    return code[:cap] + "\n-- [truncated for length] --"


def build_fix_messages(
    code: str,
    issues: List[Issue],
    *,
    pipeline_format: Optional[str] = None,
    dialect: Optional[str] = None,
) -> List[dict]:
    """Build the ``[system, user]`` chat messages for the fix-generation call.

    Args:
        code: The raw pipeline source to rewrite.
        issues: The fixable issues to resolve, already ranked/limited by the
            caller (only the first :data:`MAX_ISSUES_IN_PROMPT` are described).
        pipeline_format: Optional detected format (``sql``/``dbt``/...) so the
            model is reminded to keep the same artifact shape.
        dialect: Optional SQL dialect to preserve.

    Returns:
        A two-message ``[system, user]`` list ready for ``stream_completion``.
    """
    fmt_note = ""
    if pipeline_format:
        fmt_note = f" The pipeline format is {pipeline_format}"
        if dialect:
            fmt_note += f" ({dialect} dialect)"
        fmt_note += "; keep it unchanged."

    user_content = (
        "Fix the following data pipeline so that every issue below is fully "
        f"resolved.{fmt_note}\n\n"
        "Issues to fix:\n"
        f"{_format_issues(issues)}\n\n"
        "Current pipeline code:\n"
        f"{_cap_source(code)}\n\n"
        "Return ONLY the complete corrected pipeline code - no prose, no "
        "explanation, and no markdown fences."
    )
    return [
        {"role": "system", "content": FIX_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
