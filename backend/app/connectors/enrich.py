"""Bridge: ground a deterministic analysis in a live database connection.

This is where "agentic workflows" meet "database management": given a connector
and an analysis, resolve the IR's tables to their REAL column schemas (so
``SELECT *`` and ambiguous references become concrete) and replace heuristic
cost with REAL profiled cost (DuckDB EXPLAIN ANALYZE, Postgres EXPLAIN,
BigQuery dry-run = exact billed bytes, Snowflake EXPLAIN). All read-only.
"""
from __future__ import annotations

import logging
from typing import List

from app.connectors.base import Connector, ConnectorUnavailable
from app.engines import pricing
from app.schemas.ir import ParseResult
from app.schemas.report import AnalysisReport

logger = logging.getLogger(__name__)


def _candidate_names(name: str) -> List[str]:
    """Names to try against the connector (qualified, then bare)."""
    parts = [p for p in name.split(".") if p]
    out = [name]
    if len(parts) > 1:
        out.append(".".join(parts[-2:]))  # schema.table
        out.append(parts[-1])  # bare table
    return list(dict.fromkeys(out))


def _real_cost_usd(stat, warehouse: str) -> float | None:
    """Translate profiled bytes into dollars via the calibrated pricing model."""
    if stat.cost_usd is not None:
        return round(float(stat.cost_usd), 4)
    if stat.bytes_scanned is None:
        return None
    try:
        wh = (warehouse or "").lower()
        if wh == "bigquery":
            usd, _ = pricing.bigquery_cost(stat.bytes_scanned)
        else:
            # snowflake (and a reasonable fallback for other warehouses)
            usd, _ = pricing.snowflake_cost(stat.bytes_scanned)
        return round(float(usd), 4)
    except Exception:  # pricing helper shape mismatch must not break grounding
        return None


def ground_report(
    report: AnalysisReport, parse_result: ParseResult, connector: Connector
) -> List[str]:
    """Enrich ``report`` in place using a live connection; return human notes.

    Never raises - grounding is best-effort enrichment on top of the
    deterministic analysis, never a replacement for it.
    """
    notes: List[str] = []

    # 1) Resolve real column schemas onto the IR tables (and lineage nodes).
    grounded_tables = 0
    grounded_columns = 0
    try:
        ir = report.ir
        node_by_id = {n.id: n for n in report.lineage.nodes} if report.lineage else {}
        for table in (ir.tables if ir else []):
            real = None
            for cand in _candidate_names(table.name):
                try:
                    real = connector.get_schema(cand)
                    break
                except ConnectorUnavailable:
                    continue
                except Exception:  # noqa: BLE001 - any driver error -> skip table
                    continue
            if real is None or not real.columns:
                continue
            cols = [c.name for c in real.columns]
            table.columns = cols
            grounded_tables += 1
            grounded_columns += len(cols)
            node = node_by_id.get(table.name)
            if node is not None:
                node.columns = cols
        if grounded_tables:
            notes.append(
                f"Resolved real schemas for {grounded_tables} table(s) "
                f"({grounded_columns} columns) from the live "
                f"{connector.kind} connection."
            )
    except Exception:
        logger.exception("Schema grounding failed")

    # 2) Replace heuristic cost with a REAL profiled measurement of the query.
    try:
        stat = connector.profile_query(parse_result.source)
        real_usd = _real_cost_usd(stat, connector.warehouse)
        bits = []
        if stat.bytes_scanned is not None:
            bits.append(f"~{stat.bytes_scanned:,} bytes")
        if stat.rows_produced is not None:
            bits.append(f"{stat.rows_produced:,} rows")
        if stat.elapsed_ms is not None:
            bits.append(f"{stat.elapsed_ms} ms")
        measured = ", ".join(bits) if bits else "profile captured"
        if stat.bytes_scanned is not None:
            report.cost_analysis.bytes_billed = int(stat.bytes_scanned)
        line = (
            f"Live {connector.kind} profiling (read-only EXPLAIN/dry-run): "
            f"{measured}"
        )
        if real_usd is not None:
            line += f" ~= ${real_usd:,.4f}/run at list price"
        report.cost_analysis.reasoning.insert(0, line)
        notes.append(line)
    except ConnectorUnavailable as exc:
        notes.append(f"Live cost profiling unavailable: {exc}")
    except Exception:
        logger.exception("Cost grounding failed")

    if not notes:
        notes.append(
            f"Connected to {connector.kind}, but none of the pipeline's tables "
            "matched the live database."
        )
    return notes
