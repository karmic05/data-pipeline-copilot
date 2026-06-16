"use client";

/**
 * Issues tab: severity + category filters over severity-sorted issues,
 * expandable cards with fix diffs and a streaming LLM explanation.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useAnalysis } from "@/lib/store";
import { streamTask } from "@/lib/api";
import type { Issue, Severity } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { SeverityBadge } from "@/components/ui/severity-badge";
import { Tooltip } from "@/components/ui/tooltip";
import { CodeBlock } from "@/components/ui/code-block";
import { StreamText } from "@/components/ui/stream-text";
import { cn } from "@/lib/utils";

const DYNAMIC_TOOLTIP =
  "Advisory finding from the dynamic LLM reviewer - complements the deterministic rules, not a guaranteed rule hit.";

type SeverityFilter = "ALL" | Severity;

const SEVERITY_FILTERS: { key: SeverityFilter; label: string }[] = [
  { key: "ALL", label: "All" },
  { key: "CRITICAL", label: "Critical" },
  { key: "WARNING", label: "Warning" },
  { key: "INFO", label: "Info" },
];

function severityFilterTint(key: SeverityFilter, active: boolean): string {
  if (!active) return "bg-transparent text-ink hover:bg-paper3";
  switch (key) {
    case "CRITICAL":
      return "bg-terra text-paper2";
    case "WARNING":
      return "bg-ochre text-ink";
    case "INFO":
      return "bg-frost text-paper2";
    default:
      return "bg-ink text-paper";
  }
}

interface IssueCardProps {
  issue: Issue;
  open: boolean;
  onToggle: () => void;
  analysisId: string;
  code: string;
}

function IssueCard({ issue, open, onToggle, analysisId, code }: IssueCardProps) {
  const [streamText, setStreamText] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const abortRef = useRef<(() => void) | null>(null);

  const stopStream = () => {
    abortRef.current?.();
    abortRef.current = null;
    setStreaming(false);
  };

  // Abort any in-flight stream when the card collapses or unmounts.
  useEffect(() => {
    if (!open && abortRef.current) {
      abortRef.current();
      abortRef.current = null;
      setStreaming(false);
    }
  }, [open]);

  useEffect(() => {
    return () => {
      abortRef.current?.();
      abortRef.current = null;
    };
  }, []);

  const explain = () => {
    setStreamText("");
    setStreamError(null);
    setStreaming(true);
    abortRef.current = streamTask(
      { analysis_id: analysisId, task: "issue", issue_id: issue.id, code },
      {
        onToken: (t) => setStreamText((prev) => prev + t),
        onDone: () => {
          setStreaming(false);
          abortRef.current = null;
        },
        onError: (msg) => {
          setStreamError(msg);
          setStreaming(false);
          abortRef.current = null;
        },
      },
    );
  };

  const imp = issue.impact;

  return (
    <Card>
      <CardContent>
        <div className="flex items-start gap-3">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            aria-label={open ? "Collapse issue" : "Expand issue"}
            className="mt-0.5 shrink-0 rounded-full border-2 border-ink bg-paper2 p-1 text-ink transition-colors hover:bg-paper3"
          >
            {open ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <SeverityBadge severity={issue.severity} />
              <span className="font-medium text-ink">{issue.title}</span>
              <span className="rounded-full bg-paper3 px-2 py-0.5 font-mono text-xs text-inksoft">
                {issue.rule}
              </span>
              {issue.source === "dynamic" && (
                <Tooltip content={DYNAMIC_TOOLTIP}>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="rounded-full border border-plum/40 bg-plum/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-plum">
                      AI
                    </span>
                    {issue.confidence != null && (
                      <span className="font-mono text-xs text-inksoft">
                        {Math.round(issue.confidence * 100)}% conf
                      </span>
                    )}
                  </span>
                </Tooltip>
              )}
              {issue.location && (
                <span className="rounded-full border-2 border-line px-2 py-0.5 font-mono text-xs text-inksoft">
                  L{issue.location.line}
                </span>
              )}
            </div>

            <p className="mt-2 leading-relaxed text-inksoft">{issue.message}</p>

            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <span className="uppercase tracking-wide text-inksoft">
                {issue.category}
              </span>
              {imp && (
                <span className="font-mono text-inksoft">
                  latency +{imp.latency_pct}% · cost x{imp.cost_multiplier} · fail{" "}
                  {Math.round(imp.failure_probability * 100)}%
                </span>
              )}
            </div>

            {open && (
              <div className="mt-4 space-y-4">
                {issue.fix_suggestion && (
                  <div className="border-l-4 border-ochre bg-paper3 px-4 py-3">
                    <p className="text-xs font-semibold uppercase tracking-wide text-ochre">
                      Suggested fix
                    </p>
                    <p className="mt-1 leading-relaxed text-ink">
                      {issue.fix_suggestion}
                    </p>
                  </div>
                )}

                {issue.fix_diff && (
                  <CodeBlock
                    code={issue.fix_diff}
                    language="diff"
                    title="Proposed change"
                  />
                )}

                <div className="space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={explain}
                      disabled={streaming}
                    >
                      Explain this ↗
                    </Button>
                    {streaming && (
                      <Button variant="ghost" size="sm" onClick={stopStream}>
                        Stop
                      </Button>
                    )}
                  </div>

                  {streamError && (
                    <p className="text-sm text-terra">{streamError}</p>
                  )}

                  {(streamText || streaming) && (
                    <div className="rounded-2xl border-2 border-line bg-paper2 p-4">
                      <StreamText text={streamText} streaming={streaming} />
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export default function IssuesTab() {
  const { report, code } = useAnalysis();
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter>("ALL");
  const [category, setCategory] = useState("ALL");
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());

  const issues = useMemo(() => report?.issues ?? [], [report]);

  const severityCounts = useMemo(() => {
    const counts: Record<SeverityFilter, number> = {
      ALL: issues.length,
      CRITICAL: 0,
      WARNING: 0,
      INFO: 0,
    };
    for (const it of issues) counts[it.severity] += 1;
    return counts;
  }, [issues]);

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const it of issues) set.add(it.category);
    return Array.from(set).sort();
  }, [issues]);

  const dynamicCount = useMemo(
    () => issues.filter((it) => it.source === "dynamic").length,
    [issues],
  );

  const filtered = useMemo(
    () =>
      issues.filter((it) => {
        if (severityFilter !== "ALL" && it.severity !== severityFilter)
          return false;
        if (category !== "ALL" && it.category !== category) return false;
        return true;
      }),
    [issues, severityFilter, category],
  );

  const toggle = (id: string) => {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (!report) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="font-display text-xl text-inksoft">
          Run an analysis to see issues.
        </p>
      </div>
    );
  }

  const categoryOptions = [
    { value: "ALL", label: "All categories" },
    ...categories.map((c) => ({ value: c, label: c })),
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        {SEVERITY_FILTERS.map((f) => {
          const active = severityFilter === f.key;
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => setSeverityFilter(f.key)}
              aria-pressed={active}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border-2 border-ink px-4 py-1.5 text-sm font-medium transition-colors",
                severityFilterTint(f.key, active),
              )}
            >
              <span>{f.label}</span>
              <span
                className={cn(
                  "inline-flex min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-semibold leading-5",
                  active ? "bg-paper2/30 text-current" : "bg-paper3 text-inksoft",
                )}
              >
                {severityCounts[f.key]}
              </span>
            </button>
          );
        })}

        <div className="ml-1">
          <Select
            value={category}
            onChange={setCategory}
            options={categoryOptions}
            label="Category"
          />
        </div>

        <div className="ml-auto flex items-center gap-2">
          {dynamicCount > 0 && (
            <Tooltip content={DYNAMIC_TOOLTIP}>
              <span className="rounded-full border border-plum/40 bg-plum/10 px-2.5 py-0.5 text-xs font-medium text-plum">
                {dynamicCount} AI
              </span>
            </Tooltip>
          )}
          <span className="text-sm text-inksoft">
            {filtered.length} {filtered.length === 1 ? "issue" : "issues"}
          </span>
        </div>
      </div>

      {issues.length === 0 ? (
        <div className="rounded-2xl border-2 border-sage bg-sage/10 p-6 shadow-block">
          <p className="font-display text-2xl text-sage">
            No issues found - ship it.
          </p>
          <p className="mt-2 leading-relaxed text-inksoft">
            The deterministic rule engine had nothing to flag on this pipeline.
          </p>
        </div>
      ) : filtered.length === 0 ? (
        <Card>
          <CardContent>
            <p className="text-inksoft">
              No issues match the current filters.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {filtered.map((issue) => (
            <IssueCard
              key={issue.id}
              issue={issue}
              open={openIds.has(issue.id)}
              onToggle={() => toggle(issue.id)}
              analysisId={report.id}
              code={code}
            />
          ))}
        </div>
      )}
    </div>
  );
}
