"use client";

/**
 * Right-hand workspace: a horizontally scrollable tab bar over the seven
 * analysis tabs, plus editorial empty / loading / error states.
 */
import { useEffect, useMemo, useState } from "react";
import { useAnalysis } from "@/lib/store";
import { TAB_IDS, type Severity, type TabId } from "@/lib/types";
import { ShapeBurst } from "@/components/memphis";
import { cn } from "@/lib/utils";
import ScoreTab from "@/components/tabs/score-tab";
import AgentTab from "@/components/tabs/agent-tab";
import IssuesTab from "@/components/tabs/issues-tab";
import LineageTab from "@/components/tabs/lineage-tab";
import CostTab from "@/components/tabs/cost-tab";
import ImpactTab from "@/components/tabs/impact-tab";
import ObservabilityTab from "@/components/tabs/observability-tab";
import SecurityTab from "@/components/tabs/security-tab";

const TAB_LABELS: Record<TabId, string> = {
  score: "Score",
  agent: "Agent",
  issues: "Issues",
  lineage: "Lineage",
  cost: "Cost",
  impact: "Impact",
  observability: "Observability",
  security: "Security",
};

const TAB_COMPONENTS: Record<TabId, () => React.ReactNode> = {
  score: ScoreTab,
  agent: AgentTab,
  issues: IssuesTab,
  lineage: LineageTab,
  cost: CostTab,
  impact: ImpactTab,
  observability: ObservabilityTab,
  security: SecurityTab,
};

const LOADER_STEPS = [
  "Parsing to IR…",
  "Running 85 deterministic rules…",
  "Building column lineage…",
  "Pricing the warehouse bill…",
  "Modeling production impact…",
];

const HOW_IT_WORKS = [
  "Parse to a typed IR",
  "Run 85 deterministic rules",
  "Price the bill + model the blast radius",
];

/** Worst (lowest-order) severity that appears in the issue list, if any. */
function worstSeverity(severities: Severity[]): Severity | null {
  if (severities.includes("CRITICAL")) return "CRITICAL";
  if (severities.includes("WARNING")) return "WARNING";
  if (severities.includes("INFO")) return "INFO";
  return null;
}

function severityChipTint(severity: Severity | null): string {
  switch (severity) {
    case "CRITICAL":
      return "bg-terra text-paper2";
    case "WARNING":
      return "bg-ochre text-ink";
    case "INFO":
      return "bg-frost text-paper2";
    default:
      return "bg-paper3 text-inksoft";
  }
}

function EmptyState() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="max-w-md text-center">
        <div className="mb-6 flex justify-center">
          <ShapeBurst />
        </div>
        <h2 className="font-display text-3xl leading-tight text-ink">
          Paste a pipeline. Know everything.
        </h2>
        <p className="mt-3 text-inksoft leading-relaxed">
          A deterministic copilot reads your SQL, dbt, Airflow, Spark, Flink,
          Kafka, or Great Expectations and tells you what production will do to
          it.
        </p>
        <ol className="mt-8 space-y-4 text-left">
          {HOW_IT_WORKS.map((step, i) => (
            <li key={step} className="flex items-start gap-4">
              <span className="font-display text-4xl leading-none text-frost">
                {i + 1}
              </span>
              <span className="pt-1 text-ink leading-relaxed">{step}</span>
            </li>
          ))}
        </ol>
        <p className="mt-8 text-xs text-inksoft">
          Runs locally — your code never leaves the machine.
        </p>
      </div>
    </div>
  );
}

function LoadingState() {
  const [step, setStep] = useState(0);

  useEffect(() => {
    const id = setInterval(() => {
      setStep((s) => (s + 1) % LOADER_STEPS.length);
    }, 1400);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="text-center">
        <div className="relative mx-auto mb-8 h-24 w-24">
          <span className="absolute left-0 top-0 h-10 w-10 animate-ping rounded-full border-2 border-ink bg-frost opacity-70" />
          <span className="absolute right-0 top-2 h-8 w-8 animate-bounce rounded-2xl border-2 border-ink bg-ochre" />
          <span
            className="absolute bottom-0 left-6 h-9 w-9 animate-spin rounded-full border-2 border-ink bg-terra"
            style={{ animationDuration: "2.4s" }}
          />
          <span className="absolute bottom-1 right-2 h-7 w-7 animate-pulse rounded-full border-2 border-ink bg-sage" />
        </div>
        <p
          className="font-display text-xl text-ink"
          role="status"
          aria-live="polite"
        >
          {LOADER_STEPS[step]}
        </p>
      </div>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="max-w-md rounded-2xl border-2 border-terra bg-terra/10 p-6 shadow-block">
        <p className="font-display text-xl text-terra">Analysis failed</p>
        <p className="mt-2 break-words leading-relaxed text-inksoft">
          {message}
        </p>
      </div>
    </div>
  );
}

export default function AnalysisPanel() {
  const { report, analyzing, error, activeTab, setActiveTab } = useAnalysis();

  const issueCount = report?.issues.length ?? 0;
  const issueWorst = useMemo(
    () => worstSeverity((report?.issues ?? []).map((it) => it.severity)),
    [report],
  );
  const securityHigh = report?.security.risk_level === "HIGH";

  const ActiveTab = TAB_COMPONENTS[activeTab];
  // The Agent tab runs its own analysis, so it is self-contained and must
  // render regardless of the global report / loading / error state.
  const isAgent = activeTab === "agent";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex gap-2 overflow-x-auto border-b-2 border-line px-4 py-2">
        {TAB_IDS.map((id) => {
          const active = id === activeTab;
          return (
            <button
              key={id}
              type="button"
              onClick={() => setActiveTab(id)}
              aria-pressed={active}
              className={cn(
                "flex shrink-0 items-center gap-1.5 rounded-full border-2 border-ink px-4 py-2 text-sm font-medium transition-colors",
                active
                  ? "bg-ink text-paper"
                  : "bg-transparent text-ink hover:bg-paper3",
              )}
            >
              <span>{TAB_LABELS[id]}</span>
              {id === "issues" && issueCount > 0 && (
                <span
                  className={cn(
                    "inline-flex min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-semibold leading-5",
                    severityChipTint(issueWorst),
                  )}
                >
                  {issueCount}
                </span>
              )}
              {id === "security" && securityHigh && (
                <span
                  className="inline-block h-2 w-2 rounded-full bg-terra"
                  aria-label="High security risk"
                />
              )}
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-5">
        {isAgent ? (
          <ActiveTab />
        ) : (
          <>
            {!report && !analyzing && !error && <EmptyState />}
            {analyzing && <LoadingState />}
            {!analyzing && error && <ErrorState message={error} />}
            {!analyzing && !error && report && <ActiveTab />}
          </>
        )}
      </div>
    </div>
  );
}
