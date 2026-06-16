"""DYNAMIC advisory review - the LLM layer that ADDS to the rule engine.

The deterministic 85-rule engine stays the source of truth. :func:`dynamic_review`
sends the compacted IR plus the list of already-found deterministic findings to
the configured LLM and asks for a few ADDITIONAL, genuinely novel issues the
fixed rules did not catch. Every result is mapped to an :class:`Issue` with
``source="dynamic"`` and a confidence score, deduplicated against the
deterministic findings.

This module never raises: if no provider is configured, the model errors, or its
output is malformed, it returns ``[]`` and the caller proceeds with the
authoritative deterministic report unchanged.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.llm.client import LLMUnavailable, get_provider_status, stream_completion
from app.llm.dynamic_prompts import build_dynamic_messages
from app.schemas.ir import Location, ParseResult
from app.schemas.report import AnalysisReport, Issue

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = {"CRITICAL", "WARNING", "INFO"}
_VALID_CATEGORIES = {
    "performance",
    "reliability",
    "observability",
    "maintainability",
    "security",
    "cost",
}
#: Default severity/category when the model omits or supplies an invalid value.
_DEFAULT_SEVERITY = "INFO"
_DEFAULT_CATEGORY = "maintainability"
#: Prefix applied to rule ids when the model does not supply its own.
_RULE_PREFIX = "DYNAMIC_"
#: Jaccard token-overlap threshold above which a dynamic title is treated as a
#: near-duplicate of a deterministic finding and dropped.
_DEDUP_OVERLAP = 0.6

_FIRST_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_WORD_RE = re.compile(r"[a-z0-9]+")


async def _complete(messages: List[dict]) -> str:
    """Accumulate a streamed completion into a single string."""
    parts: List[str] = []
    async for tok in stream_completion(messages):
        parts.append(tok)
    return "".join(parts)


def _tokens(text: str) -> set[str]:
    """Lowercased word-token set for cheap title similarity."""
    return set(_WORD_RE.findall((text or "").lower()))


def _is_duplicate(
    title: str,
    rule: str,
    existing_titles: List[set[str]],
    existing_rules: set[str],
) -> bool:
    """Whether a dynamic finding near-duplicates an existing deterministic one."""
    if rule and rule.lower() in existing_rules:
        return True
    title_tokens = _tokens(title)
    if not title_tokens:
        return False
    for other in existing_titles:
        if not other:
            continue
        overlap = len(title_tokens & other) / len(title_tokens | other)
        if overlap >= _DEDUP_OVERLAP:
            return True
    return False


def _extract_array(raw: str) -> List[Any]:
    """Defensively extract the first JSON array from the model output.

    Tries a direct parse first, then the first ``[...]`` slice. Returns ``[]``
    if nothing parseable is found (never raises).
    """
    text = (raw or "").strip()
    if not text:
        return []
    # Strip any stray markdown fences the model may have added despite the prompt.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    for candidate in (text, None):
        if candidate is None:
            match = _FIRST_ARRAY_RE.search(text)
            candidate = match.group(0) if match else None
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):  # tolerate a single object
            return [parsed]
    return []


def _coerce_line(value: Any) -> Optional[int]:
    """Coerce a model-supplied line number to a positive int or ``None``."""
    if isinstance(value, bool):  # bool is an int subclass; reject it
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        line = int(value.strip())
        return line if line > 0 else None
    return None


def _coerce_confidence(value: Any) -> float:
    """Coerce a model-supplied confidence into [0, 1], defaulting to 0.5."""
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, conf))


def _build_issue(item: Dict[str, Any], index: int) -> Optional[Issue]:
    """Build one :class:`Issue` from a raw model item, or ``None`` if unusable."""
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    message = str(item.get("message") or "").strip()
    if not title and not message:
        return None
    if not title:
        title = message[:80]
    if not message:
        message = title

    rule = str(item.get("rule") or "").strip()
    if not rule:
        rule = f"{_RULE_PREFIX}FINDING_{index + 1}"
    elif not rule.upper().startswith(_RULE_PREFIX):
        rule = _RULE_PREFIX + rule

    severity = str(item.get("severity") or "").strip().upper()
    if severity not in _VALID_SEVERITIES:
        severity = _DEFAULT_SEVERITY
    category = str(item.get("category") or "").strip().lower()
    if category not in _VALID_CATEGORIES:
        category = _DEFAULT_CATEGORY

    line = _coerce_line(item.get("line"))
    fix = item.get("fix_suggestion")
    fix_suggestion = str(fix).strip() if fix not in (None, "") else None

    return Issue(
        rule=rule,
        severity=severity,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        title=title,
        message=message,
        location=Location(line=line) if line is not None else None,
        fix_suggestion=fix_suggestion,
        source="dynamic",
        confidence=_coerce_confidence(item.get("confidence")),
    )


async def dynamic_review(
    report: AnalysisReport,
    pr: ParseResult,
    *,
    max_findings: int = 6,
) -> List[Issue]:
    """Surface up to ``max_findings`` ADDITIONAL advisory findings via the LLM.

    Sends the compacted IR (``pr.ir``) and the list of already-found
    deterministic findings to the configured provider, asking for novel issues
    the fixed rules did not catch. Results are validated, mapped to
    ``source="dynamic"`` :class:`Issue` objects with a confidence score, and
    deduplicated against the deterministic findings.

    Returns ``[]`` when no provider is available or on ANY error - this advisory
    layer never raises and never blocks the authoritative deterministic report.
    """
    if max_findings <= 0:
        return []
    try:
        if not get_provider_status().available:
            logger.debug("Dynamic review skipped: no LLM provider available")
            return []
    except Exception:  # a status probe failure must not break analysis
        logger.debug("Provider status probe failed; skipping dynamic review")
        return []

    try:
        messages = build_dynamic_messages(
            pr.ir, report.issues, max_findings=max_findings
        )
        raw = await _complete(messages)
    except LLMUnavailable as exc:
        logger.info("Dynamic review unavailable (%s); returning no findings", exc)
        return []
    except Exception:  # any prompt/transport/parse error degrades to no findings
        logger.exception("Dynamic review failed; returning no findings")
        return []

    try:
        items = _extract_array(raw)
    except Exception:
        logger.exception("Dynamic review: could not parse model output")
        return []

    existing_rules = {i.rule.lower() for i in report.issues}
    existing_titles = [_tokens(i.title) for i in report.issues]

    results: List[Issue] = []
    seen_dynamic_titles: List[set[str]] = []
    for index, item in enumerate(items):
        try:
            issue = _build_issue(item, index)
        except Exception:  # skip a single malformed item, keep the rest
            logger.debug("Skipping malformed dynamic finding at index %d", index)
            continue
        if issue is None:
            continue
        if _is_duplicate(issue.title, issue.rule, existing_titles, existing_rules):
            continue
        # Also dedup dynamic findings against each other.
        if _is_duplicate(issue.title, issue.rule, seen_dynamic_titles, set()):
            continue
        seen_dynamic_titles.append(_tokens(issue.title))
        results.append(issue)
        if len(results) >= max_findings:
            break

    logger.info(
        "Dynamic review produced %d advisory finding(s) (cap %d)",
        len(results),
        max_findings,
    )
    return results
