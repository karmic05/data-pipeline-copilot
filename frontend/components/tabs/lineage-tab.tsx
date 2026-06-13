"use client";

/**
 * Lineage tab — interactive column-level lineage rendered with @xyflow/react v12.
 * Builds React Flow nodes/edges from report.lineage with a hand-rolled layered
 * left-to-right layout. Reads the report from useAnalysis().
 */
import { useCallback, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Columns3, Download, Copy, GitBranch } from "lucide-react";
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useAnalysis } from "@/lib/store";
import type {
  LineageGraph,
  LineageNode,
  LineageNodeType,
  LineageEdge,
} from "@/lib/types";

// ── Node visuals ─────────────────────────────────────────────────────────────

const RAIL_CLASS: Record<LineageNodeType, string> = {
  source: "bg-frost",
  model: "bg-plum",
  table: "bg-ochre",
  task: "bg-ink",
  output: "bg-sage",
};

const CHIP_CLASS: Record<LineageNodeType, string> = {
  source: "bg-frost/15 text-frost",
  model: "bg-plum/15 text-plum",
  table: "bg-ochre/15 text-ochre",
  task: "bg-ink/10 text-ink",
  output: "bg-sage/15 text-sage",
};

interface LineageNodeData extends Record<string, unknown> {
  label: string;
  nodeType: LineageNodeType;
  schema_name: string | null;
  columns: string[];
  showColumns: boolean;
}

type FlowNode = Node<LineageNodeData>;

function LineageFlowNode({ data, selected }: NodeProps<FlowNode>) {
  const { label, nodeType, schema_name, columns, showColumns } = data;
  const shown = columns.slice(0, 8);
  const extra = columns.length - shown.length;
  return (
    <div
      className={`relative flex min-w-[180px] max-w-[240px] overflow-hidden rounded-xl border-2 border-ink bg-paper2 shadow-block-sm ${
        selected ? "ring-2 ring-frost ring-offset-2 ring-offset-paper" : ""
      }`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="opacity-0"
      />
      <span
        className={`w-[6px] shrink-0 ${RAIL_CLASS[nodeType]}`}
        aria-hidden="true"
      />
      <div className="flex-1 px-3 py-2">
        {schema_name && (
          <div className="font-mono text-[10px] uppercase tracking-wider text-inksoft">
            {schema_name}
          </div>
        )}
        <div className="flex items-start justify-between gap-2">
          <span className="break-words font-medium leading-snug text-ink">
            {label}
          </span>
        </div>
        <span
          className={`mt-1 inline-block rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide ${CHIP_CLASS[nodeType]}`}
        >
          {nodeType}
        </span>
        {showColumns && columns.length > 0 && (
          <ul className="mt-2 space-y-0.5 border-t border-line pt-2">
            {shown.map((c) => (
              <li
                key={c}
                className="truncate font-mono text-[11px] text-inksoft"
              >
                {c}
              </li>
            ))}
            {extra > 0 && (
              <li className="font-mono text-[11px] font-semibold text-frost">
                +{extra} more
              </li>
            )}
          </ul>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="opacity-0"
      />
    </div>
  );
}

// ── Layout ───────────────────────────────────────────────────────────────────

/**
 * Hand-rolled layered left-to-right layout. Computes in-degree, BFS depth from
 * roots (in-degree 0; if none, node index 0), x = depth*300, y = index within
 * layer * row gap. Ordering is deterministic by id.
 */
function layoutNodes(
  graph: LineageGraph,
  showColumns: boolean,
): FlowNode[] {
  const nodes = [...graph.nodes].sort((a, b) => a.id.localeCompare(b.id));
  const ids = nodes.map((n) => n.id);
  const idSet = new Set(ids);

  const inDegree = new Map<string, number>();
  const adj = new Map<string, string[]>();
  for (const id of ids) {
    inDegree.set(id, 0);
    adj.set(id, []);
  }
  for (const e of graph.edges) {
    if (!idSet.has(e.source) || !idSet.has(e.target)) continue;
    inDegree.set(e.target, (inDegree.get(e.target) ?? 0) + 1);
    adj.get(e.source)?.push(e.target);
  }

  let roots = ids.filter((id) => (inDegree.get(id) ?? 0) === 0);
  if (roots.length === 0 && ids.length > 0) roots = [ids[0]];
  roots.sort((a, b) => a.localeCompare(b));

  // BFS depth assignment (longest-path style via relaxation keeps layers clean).
  const depth = new Map<string, number>();
  for (const r of roots) depth.set(r, 0);
  const queue = [...roots];
  while (queue.length > 0) {
    const cur = queue.shift() as string;
    const d = depth.get(cur) ?? 0;
    for (const next of (adj.get(cur) ?? []).slice().sort((a, b) => a.localeCompare(b))) {
      const nd = d + 1;
      if (nd > (depth.get(next) ?? -1)) {
        depth.set(next, nd);
        queue.push(next);
      }
    }
  }
  // Any node never reached (cycles / disconnected) gets depth 0.
  for (const id of ids) if (!depth.has(id)) depth.set(id, 0);

  const rowGap = showColumns ? 210 : 110;
  const layerCount = new Map<number, number>();
  const byId = new Map(nodes.map((n) => [n.id, n]));

  return ids.map((id) => {
    const node = byId.get(id) as LineageNode;
    const d = depth.get(id) ?? 0;
    const indexInLayer = layerCount.get(d) ?? 0;
    layerCount.set(d, indexInLayer + 1);
    return {
      id,
      type: "lineage",
      position: { x: d * 300, y: indexInLayer * rowGap },
      data: {
        label: node.label,
        nodeType: node.type,
        schema_name: node.schema_name,
        columns: node.columns,
        showColumns,
      },
    };
  });
}

function buildEdges(graph: LineageGraph): Edge[] {
  return graph.edges.map((e) => {
    const t = (e.transformation || "").toLowerCase();
    const animated = t.includes("aggregation") || t.includes("window");
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      type: "smoothstep",
      animated,
      style: { stroke: "#2E2620", strokeWidth: 1.5 },
      label: e.transformation || undefined,
      labelShowBg: true,
      labelBgPadding: [6, 3] as [number, number],
      labelBgBorderRadius: 8,
      labelBgStyle: { fill: "#FDFAF2", stroke: "#D8CCB8" },
      labelStyle: {
        fontFamily: "var(--font-mono), monospace",
        fontSize: 10,
        fill: "#6B5D4F",
      },
    };
  });
}

// ── Mermaid + downloads ──────────────────────────────────────────────────────

function sanitizeId(id: string): string {
  const cleaned = id.replace(/[^a-zA-Z0-9]/g, "_");
  return /^[a-zA-Z]/.test(cleaned) ? cleaned : `n_${cleaned}`;
}

function buildMermaid(graph: LineageGraph): string {
  const lines = ["graph LR"];
  for (const n of graph.nodes) {
    lines.push(`  ${sanitizeId(n.id)}["${n.label.replace(/"/g, "'")}"]`);
  }
  for (const e of graph.edges) {
    const label = e.transformation
      ? `|"${e.transformation.replace(/"/g, "'")}"|`
      : "";
    lines.push(`  ${sanitizeId(e.source)} -->${label} ${sanitizeId(e.target)}`);
  }
  return lines.join("\n");
}

function downloadBlob(content: string, filename: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

// ── Side panels ──────────────────────────────────────────────────────────────

function NodeDetail({
  node,
  graph,
  onClose,
}: {
  node: LineageNode;
  graph: LineageGraph;
  onClose: () => void;
}) {
  const labelOf = (id: string) =>
    graph.nodes.find((n) => n.id === id)?.label ?? id;
  const upstream = graph.edges
    .filter((e) => e.target === node.id)
    .map((e) => labelOf(e.source));
  const downstream = graph.edges
    .filter((e) => e.source === node.id)
    .map((e) => labelOf(e.target));

  return (
    <Card className="absolute right-3 top-3 z-10 max-h-[calc(100%-1.5rem)] w-72 overflow-auto">
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div>
          {node.schema_name && (
            <div className="font-mono text-[10px] uppercase tracking-wider text-inksoft">
              {node.schema_name}
            </div>
          )}
          <CardTitle className="text-lg">{node.label}</CardTitle>
          <Badge tone="frost">{node.type}</Badge>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close details">
          ✕
        </Button>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div>
          <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
            Upstream ({upstream.length})
          </div>
          {upstream.length === 0 ? (
            <p className="text-inksoft">No upstream dependencies.</p>
          ) : (
            <ul className="mt-1 space-y-1">
              {upstream.map((l, i) => (
                <li key={`${l}-${i}`} className="font-medium text-ink">
                  {l}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
            Downstream ({downstream.length})
          </div>
          {downstream.length === 0 ? (
            <p className="text-inksoft">No downstream consumers.</p>
          ) : (
            <ul className="mt-1 space-y-1">
              {downstream.map((l, i) => (
                <li key={`${l}-${i}`} className="font-medium text-ink">
                  {l}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-xl bg-paper3 px-3 py-2">
          <span className="font-display text-2xl text-frost">
            {node.columns.length}
          </span>{" "}
          <span className="text-inksoft">columns tracked</span>
        </div>
      </CardContent>
    </Card>
  );
}

function EdgeDetail({
  edge,
  graph,
  onClose,
}: {
  edge: LineageEdge;
  graph: LineageGraph;
  onClose: () => void;
}) {
  const labelOf = (id: string) =>
    graph.nodes.find((n) => n.id === id)?.label ?? id;

  return (
    <Card className="absolute right-3 top-3 z-10 max-h-[calc(100%-1.5rem)] w-80 overflow-auto">
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div>
          <CardTitle className="text-lg">
            {labelOf(edge.source)} → {labelOf(edge.target)}
          </CardTitle>
          {edge.transformation && (
            <Badge tone="ochre">{edge.transformation}</Badge>
          )}
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close details">
          ✕
        </Button>
      </CardHeader>
      <CardContent className="text-sm">
        <div className="mb-2 font-mono text-[11px] uppercase tracking-wider text-inksoft">
          Column links ({edge.column_links.length})
        </div>
        {edge.column_links.length === 0 ? (
          <p className="text-inksoft">
            No column-level links were inferred for this edge.
          </p>
        ) : (
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="border-b-2 border-line">
                <th className="py-1 font-mono text-[10px] uppercase tracking-wider text-inksoft">
                  From
                </th>
                <th className="py-1 font-mono text-[10px] uppercase tracking-wider text-inksoft">
                  To
                </th>
                <th className="py-1 font-mono text-[10px] uppercase tracking-wider text-inksoft">
                  Transform
                </th>
              </tr>
            </thead>
            <tbody>
              {edge.column_links.map((link, i) => (
                <tr key={i} className="border-b border-line/60">
                  <td className="py-1 pr-2 font-mono text-[11px] text-ink">
                    {link.from_column}
                  </td>
                  <td className="py-1 pr-2 font-mono text-[11px] text-ink">
                    {link.to_column}
                  </td>
                  <td className="py-1 font-mono text-[11px] text-inksoft">
                    {link.transformation || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

// ── Main tab ─────────────────────────────────────────────────────────────────

export default function LineageTab() {
  const { report } = useAnalysis();
  const [showColumns, setShowColumns] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const graph = report?.lineage;

  const nodeTypes = useMemo<NodeTypes>(
    () => ({ lineage: LineageFlowNode }),
    [],
  );

  const nodes = useMemo<FlowNode[]>(
    () => (graph ? layoutNodes(graph, showColumns) : []),
    [graph, showColumns],
  );
  const edges = useMemo<Edge[]>(
    () => (graph ? buildEdges(graph) : []),
    [graph],
  );

  const onNodeClick = useCallback((_: unknown, node: { id: string }) => {
    setSelectedNodeId(node.id);
    setSelectedEdgeId(null);
  }, []);
  const onEdgeClick = useCallback((_: unknown, edge: { id: string }) => {
    setSelectedEdgeId(edge.id);
    setSelectedNodeId(null);
  }, []);
  const onPaneClick = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  }, []);

  if (!report || !graph) {
    return (
      <Card className="mx-auto mt-8 max-w-md text-center">
        <CardContent className="space-y-3 py-10">
          <GitBranch className="mx-auto h-10 w-10 text-frost" />
          <CardTitle className="text-xl">No lineage yet</CardTitle>
          <p className="text-inksoft">
            Analyze a pipeline to map how data flows column-by-column across
            sources, models, and outputs.
          </p>
        </CardContent>
      </Card>
    );
  }

  const selectedNode =
    selectedNodeId != null
      ? graph.nodes.find((n) => n.id === selectedNodeId) ?? null
      : null;
  const selectedEdge =
    selectedEdgeId != null
      ? graph.edges.find((e) => e.id === selectedEdgeId) ?? null
      : null;

  const handleCopyMermaid = async () => {
    const mermaid = buildMermaid(graph);
    try {
      await navigator.clipboard.writeText(mermaid);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      downloadBlob(mermaid, "lineage.mmd", "text/plain");
    }
  };

  return (
    <div className="flex h-full flex-col gap-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => setShowColumns((v) => !v)}
          aria-pressed={showColumns}
          className={`inline-flex items-center gap-2 rounded-full border-2 border-ink px-3 py-1.5 text-sm font-medium shadow-block-sm transition-colors ${
            showColumns ? "bg-frost text-paper2" : "bg-paper2 text-ink"
          }`}
        >
          <span
            className={`flex h-4 w-4 items-center justify-center rounded-sm border-2 border-ink text-[10px] ${
              showColumns ? "bg-paper2 text-frost" : "bg-paper2 text-transparent"
            }`}
          >
            ✓
          </span>
          <Columns3 className="h-4 w-4" />
          Columns
        </button>

        <div className="font-mono text-xs text-inksoft">
          {graph.nodes.length} nodes · {graph.edges.length} edges
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              downloadBlob(
                JSON.stringify(graph, null, 2),
                "lineage.json",
                "application/json",
              )
            }
          >
            <Download className="mr-1.5 h-3.5 w-3.5" />
            JSON
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              downloadBlob(buildMermaid(graph), "lineage.mmd", "text/plain")
            }
          >
            <Download className="mr-1.5 h-3.5 w-3.5" />
            Mermaid
          </Button>
          <Button variant="outline" size="sm" onClick={handleCopyMermaid}>
            <Copy className="mr-1.5 h-3.5 w-3.5" />
            {copied ? "Copied!" : "Copy Mermaid"}
          </Button>
        </div>
      </div>

      {/* Canvas */}
      {nodes.length === 0 ? (
        <Card className="flex flex-1 items-center justify-center text-center">
          <CardContent className="space-y-2 py-10">
            <CardTitle className="text-lg">No lineage nodes</CardTitle>
            <p className="text-inksoft">
              The parser did not resolve any tables or models from this
              pipeline.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="relative h-full min-h-[460px] flex-1 overflow-hidden rounded-2xl border-2 border-line bg-paper">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            proOptions={{ hideAttribution: true }}
            onNodeClick={onNodeClick}
            onEdgeClick={onEdgeClick}
            onPaneClick={onPaneClick}
            nodesDraggable={false}
            nodesConnectable={false}
          >
            <Background variant={BackgroundVariant.Dots} color="#D8CCB8" />
            <Controls />
          </ReactFlow>

          {selectedNode && (
            <NodeDetail
              node={selectedNode}
              graph={graph}
              onClose={() => setSelectedNodeId(null)}
            />
          )}
          {selectedEdge && (
            <EdgeDetail
              edge={selectedEdge}
              graph={graph}
              onClose={() => setSelectedEdgeId(null)}
            />
          )}
        </div>
      )}
    </div>
  );
}
