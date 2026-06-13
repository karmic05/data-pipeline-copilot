"""Lineage graph construction.

Transforms the unified :class:`~app.schemas.ir.IR` into a renderable
:class:`~app.schemas.report.LineageGraph`:

- **Nodes** come from ``ir.tables``. Read-only tables become ``source`` nodes
  (names hinting at raw/source/stg layers reinforce that role), final write
  targets become ``output`` nodes, intermediate read+write tables become
  ``model`` nodes, and anything else falls back to ``table``. ``schema_name``
  and ``columns`` are carried over.
- For task-based IRs (Airflow / Prefect / Kafka) dependency endpoints that are
  not tables become ``task`` nodes.
- **Edges** come from ``ir.dependencies`` (deduplicated by source+target,
  ids shaped like ``e-src-tgt``). Column-level lineage rows are grouped per
  ``(source_table -> output_table)`` pair and attached as ``column_links``;
  the edge ``transformation`` label is the dominant column transformation
  (``direct`` only when every link is direct) and falls back to the
  dependency type when no column links exist.

The graph never has dangling edge endpoints (missing nodes are synthesized)
and is never empty when the IR carries any signal — as a last resort a single
node is synthesized from ``ir.metadata.name``.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Dict, List, Literal, Set, Tuple

from app.schemas.ir import IR, TableRef
from app.schemas.report import (
    LineageColumnLink,
    LineageEdge,
    LineageGraph,
    LineageNode,
)

logger = logging.getLogger(__name__)

NodeType = Literal["source", "model", "table", "task", "output"]

_TASK_FORMATS = frozenset({"airflow", "prefect", "kafka"})
_SOURCE_NAME_HINTS = frozenset(
    {"raw", "source", "sources", "src", "stg", "staging", "landing", "land", "seed"}
)
_SANITIZE_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _sanitize(name: str) -> str:
    """Lower-case ``name`` and collapse every non-alphanumeric run to ``-``."""
    slug = _SANITIZE_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "node"


def _has_source_hint(name: str) -> bool:
    """True when a table/topic name suggests a raw / source / staging layer."""
    tokens = [tok for tok in _TOKEN_SPLIT_RE.split(name.lower()) if tok]
    return any(
        tok in _SOURCE_NAME_HINTS or tok.startswith(("raw", "stg", "src"))
        for tok in tokens
    )


def _merge_tables(tables: List[TableRef]) -> Dict[str, TableRef]:
    """Merge duplicate table refs by name, combining access types and columns.

    A table seen with both ``read`` and ``write`` access collapses to
    ``readwrite``. Column lists are unioned preserving first-seen order.
    Insertion order (and therefore node order) is deterministic.
    """
    merged: Dict[str, TableRef] = {}
    for table in tables:
        name = (table.name or "").strip()
        if not name:
            logger.debug("Skipping table ref with empty name: %r", table)
            continue
        existing = merged.get(name)
        if existing is None:
            merged[name] = table.model_copy(deep=True)
            continue
        if existing.access_type != table.access_type:
            existing.access_type = "readwrite"
        for column in table.columns:
            if column not in existing.columns:
                existing.columns.append(column)
        existing.schema_name = existing.schema_name or table.schema_name
        existing.alias = existing.alias or table.alias
    return merged


def _classify_table(table: TableRef, *, has_downstream_readers: bool) -> NodeType:
    """Classify a table ref into a lineage node type.

    - read-only             -> ``source`` (raw/source/stg name hints agree)
    - read+write            -> ``model`` (intermediate)
    - write-only, terminal  -> ``output`` (final write target)
    - write-only, re-read   -> ``model``
    - anything else         -> ``table``
    """
    if table.access_type == "read":
        return "source"
    if table.access_type == "readwrite":
        return "model"
    if table.access_type == "write":
        return "model" if has_downstream_readers else "output"
    return "table"


def _dominant_transformation(links: List[LineageColumnLink]) -> str:
    """The dominant column transformation for an edge.

    ``direct`` only when every link is direct; otherwise the most frequent
    non-direct transformation, with deterministic tie-breaking that prefers
    the heavier transformation (aggregation > window > expression).
    """
    if all(link.transformation == "direct" for link in links):
        return "direct"
    counts = Counter(
        link.transformation
        for link in links
        if link.transformation not in ("direct", "unknown")
    )
    if not counts:
        return "expression"
    priority = {"aggregation": 0, "window": 1, "expression": 2}
    return min(counts, key=lambda kind: (-counts[kind], priority.get(kind, 9), kind))


def build_lineage(ir: IR) -> LineageGraph:
    """Build a :class:`LineageGraph` from the IR.

    Pure and deterministic: node order follows ``ir.tables`` (then synthesized
    dependency endpoints), edge order follows ``ir.dependencies`` (then
    column-lineage-only pairs). Malformed entries (empty names/endpoints) are
    skipped with a debug log instead of failing the analysis.
    """
    nodes: Dict[str, LineageNode] = {}
    edges: Dict[Tuple[str, str], LineageEdge] = {}
    used_edge_ids: Set[str] = set()

    def make_edge_id(source: str, target: str) -> str:
        base = f"e-{_sanitize(source)}-{_sanitize(target)}"
        edge_id, suffix = base, 2
        while edge_id in used_edge_ids:
            edge_id = f"{base}-{suffix}"
            suffix += 1
        used_edge_ids.add(edge_id)
        return edge_id

    def ensure_node(name: str) -> None:
        if name in nodes:
            return
        node_type: NodeType
        if ir.format in _TASK_FORMATS:
            node_type = "task"
        elif _has_source_hint(name):
            node_type = "source"
        else:
            node_type = "table"
        nodes[name] = LineageNode(id=name, label=name, type=node_type)

    # Names that are read onward (appear as an edge source) feed downstream
    # consumers, which demotes a written table from "output" to "model".
    read_onward: Set[str] = {
        dep.source.strip() for dep in ir.dependencies if (dep.source or "").strip()
    }
    read_onward |= {
        cl.source_table.strip() for cl in ir.column_lineage if (cl.source_table or "").strip()
    }

    # --- nodes from ir.tables ------------------------------------------------
    for name, table in _merge_tables(ir.tables).items():
        nodes[name] = LineageNode(
            id=name,
            label=name,
            type=_classify_table(table, has_downstream_readers=name in read_onward),
            schema_name=table.schema_name,
            columns=list(table.columns),
        )

    # --- edges from ir.dependencies (deduped by source+target) ---------------
    for dep in ir.dependencies:
        source = (dep.source or "").strip()
        target = (dep.target or "").strip()
        if not source or not target:
            logger.debug("Skipping dependency with empty endpoint: %r", dep)
            continue
        key = (source, target)
        if key in edges:
            continue
        ensure_node(source)
        ensure_node(target)
        edges[key] = LineageEdge(
            id=make_edge_id(source, target),
            source=source,
            target=target,
            transformation=dep.type,
        )

    # --- column links grouped per (source_table -> output_table) pair --------
    column_groups: Dict[Tuple[str, str], List[LineageColumnLink]] = {}
    for cl in ir.column_lineage:
        source = (cl.source_table or "").strip()
        target = (cl.output_table or "").strip()
        if not source or not target or not cl.source_column or not cl.output_column:
            logger.debug("Skipping incomplete column lineage row: %r", cl)
            continue
        link = LineageColumnLink(
            from_column=cl.source_column,
            to_column=cl.output_column,
            transformation=cl.transformation,
        )
        group = column_groups.setdefault((source, target), [])
        if not any(
            existing.from_column == link.from_column
            and existing.to_column == link.to_column
            and existing.transformation == link.transformation
            for existing in group
        ):
            group.append(link)

    for (source, target), links in column_groups.items():
        edge = edges.get((source, target))
        if edge is None:
            # Column lineage implies data flow even without an explicit
            # dependency edge — synthesize it so the links are never lost.
            ensure_node(source)
            ensure_node(target)
            edge = LineageEdge(
                id=make_edge_id(source, target),
                source=source,
                target=target,
            )
            edges[(source, target)] = edge
        edge.column_links = links
        edge.transformation = _dominant_transformation(links)

    # --- last resort: never return an empty graph ----------------------------
    if not nodes:
        label = (ir.metadata.name or "").strip() or "pipeline"
        node_type: NodeType = "task" if ir.format in _TASK_FORMATS else "model"
        nodes[label] = LineageNode(id=label, label=label, type=node_type)
        logger.debug("Synthesized single lineage node %r for empty IR", label)

    graph = LineageGraph(nodes=list(nodes.values()), edges=list(edges.values()))
    logger.debug(
        "Built lineage graph for %s IR: %d nodes, %d edges",
        ir.format,
        len(graph.nodes),
        len(graph.edges),
    )
    return graph
