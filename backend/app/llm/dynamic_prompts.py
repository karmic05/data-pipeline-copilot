"""Prompt construction for the DYNAMIC advisory review layer.

The deterministic 85-rule engine remains the source of truth. This module
builds the ``[system, user]`` chat messages that ask the LLM to surface a
handful of ADDITIONAL, genuinely novel findings the fixed rules did not catch -
semantic / business-logic smells, dialect-specific gotchas, data-correctness
risks and subtle anti-patterns - grounded strictly in the fields present in the
compacted IR.

Like the rest of the LLM layer, the model only ever sees structured IR JSON -
never raw pipeline source code.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from app.schemas.ir import IR
from app.schemas.report import Issue

logger = logging.getLogger(__name__)

#: Approximate hard cap (characters) for the serialized IR embedded in the
#: user message. Operation lists are truncated progressively to fit.
IR_JSON_CHAR_CAP = 7000

#: Valid enum values the model must choose from (kept in sync with the schema).
_SEVERITIES = ("CRITICAL", "WARNING", "INFO")
_CATEGORIES = (
    "performance",
    "reliability",
    "observability",
    "maintainability",
    "security",
    "cost",
)

DYNAMIC_SYSTEM_PROMPT: str = (
    "You are a staff data engineer doing a SECOND-PASS review on top of an "
    "automated rule engine that has ALREADY run. You receive ONLY the "
    "structured intermediate representation (IR) of a pipeline as JSON - never "
    "the raw source code - plus the list of findings the deterministic rules "
    "already reported.\n"
    "\n"
    "Your job: surface ADDITIONAL, genuinely novel issues the fixed rules did "
    "NOT catch - semantic or business-logic smells, dialect-specific gotchas, "
    "data-correctness risks, and subtle anti-patterns.\n"
    "\n"
    "Hard rules:\n"
    "- Reference only tables, columns, operations and dialects that appear in "
    "the provided IR JSON. Never invent names or metrics.\n"
    "- Do NOT repeat, rephrase, or overlap with anything in the already-found "
    "list - only report things it missed.\n"
    "- Each finding must be grounded in a specific field of the IR. If you are "
    "not confident a problem is really present, leave it out.\n"
    "- Keep each message under 120 words, concrete and actionable.\n"
    "- Output STRICT JSON only: a single JSON array, no prose, no markdown "
    "fences. If you find nothing novel, output []."
)


def _schema_instruction(max_findings: int) -> str:
    """The per-request instruction describing the required JSON array shape."""
    return (
        f"Return UP TO {max_findings} additional findings as a JSON array. "
        "Each element MUST be an object with exactly these keys:\n"
        '  "rule": short stable id string (e.g. "DYNAMIC_IMPLICIT_TZ"),\n'
        f'  "severity": one of {list(_SEVERITIES)},\n'
        f'  "category": one of {list(_CATEGORIES)},\n'
        '  "title": short headline (<=80 chars),\n'
        '  "message": explanation grounded in the IR (<120 words),\n'
        '  "line": integer line number from the IR, or null,\n'
        '  "fix_suggestion": one concrete remediation sentence,\n'
        '  "confidence": float 0-1 (your confidence the issue is real).\n'
        "Output ONLY the JSON array."
    )


def _compact_ir(ir: IR) -> Dict[str, Any]:
    """Build a slim, JSON-serializable view of the IR for the prompt.

    Carries tables (name/schema/columns/access), the operation mix with their
    rule-relevant ``details`` and line numbers, dependencies, column lineage,
    scheduling and materialization - never raw source code.
    """
    return {
        "format": ir.format,
        "dialect": ir.dialect,
        "metadata": ir.metadata.model_dump(mode="json"),
        "tables": [
            {
                "name": t.name,
                "schema": t.schema_name,
                "database": t.database,
                "access": t.access_type,
                "columns": list(t.columns),
            }
            for t in ir.tables
        ],
        "operations": [
            {
                "type": op.type,
                "line": op.location.line if op.location else None,
                "details": op.details,
            }
            for op in ir.operations
        ],
        "dependencies": [
            {"source": d.source, "target": d.target, "type": d.type}
            for d in ir.dependencies
        ],
        "column_lineage": [
            {
                "output": f"{cl.output_table}.{cl.output_column}",
                "source": f"{cl.source_table}.{cl.source_column}",
                "transformation": cl.transformation,
                "expression": cl.expression,
            }
            for cl in ir.column_lineage
        ],
        "scheduling": ir.scheduling.model_dump(mode="json"),
        "materialization": ir.materialization.model_dump(mode="json"),
    }


def _shrink(payload: Dict[str, Any]) -> bool:
    """Apply one increasingly-aggressive truncation step in place.

    Returns ``True`` if a step was applied (so the caller should re-measure),
    ``False`` when nothing further can be trimmed.
    """
    # Trim the longest list-valued sections first, halving each pass.
    for key in ("operations", "column_lineage", "dependencies", "tables"):
        seq = payload.get(key)
        if isinstance(seq, list) and len(seq) > 4:
            payload[key] = seq[: max(4, len(seq) // 2)]
            return True
    # Then drop per-column lists, which can dominate wide tables.
    trimmed = False
    for table in payload.get("tables", []):
        cols = table.get("columns")
        if isinstance(cols, list) and len(cols) > 8:
            table["columns"] = cols[:8]
            trimmed = True
    return trimmed


def _serialize_capped(payload: Dict[str, Any], cap: int = IR_JSON_CHAR_CAP) -> str:
    """Serialize ``payload`` to compact JSON, truncating lists until it fits."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    while len(text) > cap and _shrink(payload):
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > cap:
        logger.debug("Dynamic IR context still %d chars; hard-slicing", len(text))
        return text[:cap]
    return text


def _compact_existing(existing: List[Issue], limit: int = 40) -> List[Dict[str, str]]:
    """The already-found deterministic findings (rule id + title) for dedup."""
    out: List[Dict[str, str]] = []
    for issue in existing[:limit]:
        out.append({"rule": issue.rule, "title": issue.title})
    return out


def build_dynamic_messages(
    ir: IR,
    existing: List[Issue],
    *,
    max_findings: int = 6,
) -> List[dict]:
    """Build the ``[system, user]`` chat messages for the dynamic review.

    The user message embeds the compacted IR JSON and the list of findings the
    deterministic rules already reported, then asks for up to ``max_findings``
    additional, non-overlapping issues in a strict JSON array.
    """
    ir_json = _serialize_capped(_compact_ir(ir))
    already = json.dumps(
        _compact_existing(existing), ensure_ascii=False, separators=(",", ":")
    )
    user_content = (
        f"{_schema_instruction(max_findings)}\n\n"
        "Pipeline IR (structured JSON - no source code is available):\n"
        f"```json\n{ir_json}\n```\n\n"
        "Findings the deterministic rule engine ALREADY reported (do NOT "
        "repeat or overlap with these):\n"
        f"```json\n{already}\n```"
    )
    return [
        {"role": "system", "content": DYNAMIC_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
