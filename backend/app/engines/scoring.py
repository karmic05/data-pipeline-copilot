"""Production readiness scoring engine.

Computes a weighted :class:`~app.schemas.report.ProductionScore` from the
deterministic rule-engine issues plus structural IR signals:

- Five dimensions, each 0-100, weighted: efficiency .25, reliability .25,
  observability .20, maintainability .15, security .15 (per CONTRACTS.md).
- Issue categories map to dimensions (performance & cost -> efficiency; the
  remaining categories map to their namesake dimension) and deduct per
  severity: CRITICAL 22, WARNING 7, INFO 2 (each dimension clamped at 0).
- IR-signal adjustments reward declared tests/SLAs, retries, and docs, and
  cap the security dimension at 25 when a CRITICAL security issue exists.
- The roadmap surfaces the top five fixes ranked by the total-score points
  each would recover (dimension deduction x dimension weight).

Pure and deterministic; every returned value lies in [0, 100].
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Tuple

from app.schemas.ir import IR, ParseResult
from app.schemas.report import SEVERITY_ORDER, Issue, ProductionScore, RoadmapItem

logger = logging.getLogger(__name__)

_WEIGHTS: Dict[str, float] = {
    "efficiency": 0.25,
    "reliability": 0.25,
    "observability": 0.20,
    "maintainability": 0.15,
    "security": 0.15,
}
_CATEGORY_TO_DIMENSION: Dict[str, str] = {
    "performance": "efficiency",
    "cost": "efficiency",
    "reliability": "reliability",
    "observability": "observability",
    "maintainability": "maintainability",
    "security": "security",
}
_SEVERITY_DEDUCTION: Dict[str, float] = {"CRITICAL": 22.0, "WARNING": 7.0, "INFO": 2.0}
_DEFAULT_DIMENSION = "maintainability"
_SECURITY_CRITICAL_CAP = 25.0
_MAX_ROADMAP_ITEMS = 5
_ACTION_MAX_LEN = 140

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def _dimension_for(issue: Issue) -> str:
    """Map an issue's category to its scoring dimension."""
    dimension = _CATEGORY_TO_DIMENSION.get(issue.category)
    if dimension is None:
        logger.warning(
            "Issue %s has unmapped category %r; scoring against %s",
            issue.rule,
            issue.category,
            _DEFAULT_DIMENSION,
        )
        return _DEFAULT_DIMENSION
    return dimension


def _count_column_aliases(ir: IR) -> int:
    """Deterministic alias-effort proxy: table aliases + renamed output columns."""
    count = sum(1 for table in ir.tables if table.alias)
    count += sum(
        1
        for cl in ir.column_lineage
        if cl.output_column and cl.source_column and cl.output_column != cl.source_column
    )
    return count


def _first_sentence(text: str) -> str:
    """First sentence of ``text``, whitespace-normalized and length-capped."""
    text = " ".join(text.split())
    text = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)[0].rstrip(".")
    if len(text) > _ACTION_MAX_LEN:
        text = text[: _ACTION_MAX_LEN - 1].rstrip() + "…"
    return text


def _build_action(issue: Issue) -> str:
    """Concrete imperative action for the roadmap, ending with the rule id.

    Prefers the rule's ``fix_suggestion`` (already object-specific and
    imperative); otherwise combines the rule title with the issue message,
    e.g. ``"Add ON clause to the orders x customers join (CARTESIAN_JOIN)"``.
    """
    suggestion = (issue.fix_suggestion or "").strip()
    if suggestion:
        action = _first_sentence(suggestion)
    else:
        title = (issue.title or issue.rule.replace("_", " ").title()).strip()
        detail = _first_sentence(issue.message) if (issue.message or "").strip() else ""
        action = f"Fix {title}: {detail}" if detail else f"Fix {title}"
    return f"{action} ({issue.rule})"


def _build_roadmap(issues: List[Issue]) -> List[RoadmapItem]:
    """Top fixes ranked by total-score points recovered.

    ``estimated_gain`` = severity deduction x dimension weight, expressed in
    total-score points and rounded to one decimal. Ties break by severity,
    rule id, then original issue order for full determinism.
    """
    ranked: List[Tuple[float, int, str, int, RoadmapItem]] = []
    for index, issue in enumerate(issues):
        dimension = _dimension_for(issue)
        deduction = _SEVERITY_DEDUCTION.get(issue.severity, _SEVERITY_DEDUCTION["INFO"])
        gain = round(deduction * _WEIGHTS[dimension], 1)
        item = RoadmapItem(
            dimension=dimension,
            action=_build_action(issue),
            estimated_gain=gain,
        )
        ranked.append(
            (-gain, SEVERITY_ORDER.get(issue.severity, 9), issue.rule, index, item)
        )
    ranked.sort(key=lambda entry: entry[:4])
    return [entry[4] for entry in ranked[:_MAX_ROADMAP_ITEMS]]


def compute_score(issues: List[Issue], pr: ParseResult) -> ProductionScore:
    """Compute the weighted production readiness score for a pipeline.

    Args:
        issues: Issues emitted by the rule engine (any order).
        pr: The parse result whose IR / extras supply structural signals.

    Returns:
        A :class:`ProductionScore` with all five dimensions and the total in
        ``[0, 100]`` plus a roadmap of up to five highest-impact fixes.
    """
    dims: Dict[str, float] = {name: 100.0 for name in _WEIGHTS}

    # --- per-issue deductions -------------------------------------------------
    for issue in issues:
        dimension = _dimension_for(issue)
        deduction = _SEVERITY_DEDUCTION.get(issue.severity, _SEVERITY_DEDUCTION["INFO"])
        dims[dimension] = max(0.0, dims[dimension] - deduction)

    # --- IR-signal adjustments --------------------------------------------------
    ir = pr.ir
    has_tests = bool(pr.extras.get("tests"))
    has_sla = ir.scheduling.sla_minutes is not None
    if has_tests or has_sla:
        dims["observability"] += 10.0
    else:
        dims["observability"] -= 10.0

    retries = ir.scheduling.retries
    if retries is not None and retries >= 2:
        dims["reliability"] += 5.0

    has_doc = bool((ir.metadata.description or "").strip())
    if has_doc:
        dims["maintainability"] += 5.0
    elif _count_column_aliases(ir) == 0:
        dims["maintainability"] -= 5.0

    dims = {name: _clamp(value) for name, value in dims.items()}

    # --- security floor: any CRITICAL security issue caps the dimension --------
    if any(i.category == "security" and i.severity == "CRITICAL" for i in issues):
        dims["security"] = min(dims["security"], _SECURITY_CRITICAL_CAP)

    total = _clamp(
        float(round(sum(dims[name] * weight for name, weight in _WEIGHTS.items())))
    )

    score = ProductionScore(
        total=total,
        efficiency=dims["efficiency"],
        reliability=dims["reliability"],
        observability=dims["observability"],
        maintainability=dims["maintainability"],
        security=dims["security"],
        roadmap=_build_roadmap(issues),
    )
    logger.debug(
        "Scored %s pipeline: total=%.0f from %d issues (%s)",
        ir.format,
        score.total,
        len(issues),
        ", ".join(f"{name}={dims[name]:.0f}" for name in _WEIGHTS),
    )
    return score
