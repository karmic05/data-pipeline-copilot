"use client";

/**
 * Agent tab - the UI for the autonomous pipeline-doctor agent. It runs its own
 * durable workflow (analyze → review → propose & apply fixes → re-analyze →
 * measure impact) and reports operational (AgentKPIs) + business (BusinessKPIs)
 * outcomes. Self-contained: reads { code, params } from useAnalysis() and calls
 * runAgent() directly, so it works before any global analysis exists.
 */
import { useEffect, useRef, useState } from "react";
import {
  Bot,
  Sparkles,
  Check,
  X,
  Minus,
  Circle,
  Loader2,
  ArrowRight,
  ShieldCheck,
  TrendingUp,
  Database,
} from "lucide-react";
import { useAnalysis } from "@/lib/store";
import { runAgent } from "@/lib/api";
import type { AgentRun, StepStatus, WorkflowStep } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Meter, type MeterTone } from "@/components/ui/progress";
import { ShapeBurst } from "@/components/memphis";
import { cn } from "@/lib/utils";

// ── Formatters ───────────────────────────────────────────────────────────────

const fmtUSD = (v: number): string =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(v);

const fmtInt = (v: number): string =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(v);

/** Render a duration in ms as "{ms} ms" under 1s, else "{s.s}s". */
function fmtDuration(ms: number | null): string {
  if (ms == null) return "-";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

const ROTATING_STATUS = [
  "Analyzing…",
  "Reviewing dynamically…",
  "Proposing fixes…",
  "Re-analyzing…",
  "Measuring impact…",
];

// ── Toning ───────────────────────────────────────────────────────────────────

/** A readiness score (0-100) → design tone for its Meter / stat. */
function toneForScore(score: number): MeterTone {
  if (score >= 80) return "sage";
  if (score >= 50) return "ochre";
  return "terra";
}

// ── Run button (shared between empty + populated states) ─────────────────────

function RunButton({
  running,
  onClick,
  label,
}: {
  running: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <Button onClick={onClick} loading={running} className="gap-2">
      {!running && <Bot aria-hidden="true" className="h-4 w-4" />}
      {running ? "Running agent…" : label}
      {!running && <Sparkles aria-hidden="true" className="h-4 w-4" />}
    </Button>
  );
}

// ── Rotating status line (while a run is in flight) ──────────────────────────

function RunningStatus() {
  const [i, setI] = useState(0);
  useEffect(() => {
    const id = setInterval(
      () => setI((s) => (s + 1) % ROTATING_STATUS.length),
      1300,
    );
    return () => clearInterval(id);
  }, []);
  return (
    <div className="flex items-center gap-3 rounded-2xl border-2 border-line bg-paper3 px-4 py-3">
      <Loader2 aria-hidden="true" className="h-5 w-5 animate-spin text-frost" />
      <p
        className="font-display text-lg text-ink"
        role="status"
        aria-live="polite"
      >
        {ROTATING_STATUS[i]}
      </p>
    </div>
  );
}

// ── Business-KPI stat card ───────────────────────────────────────────────────

function StatCard({
  label,
  value,
  tone = "ink",
  sub,
}: {
  label: string;
  value: string;
  tone?: "ink" | "sage" | "terra" | "frost";
  sub?: string;
}) {
  const valueTone =
    tone === "sage"
      ? "text-sage"
      : tone === "terra"
        ? "text-terra"
        : tone === "frost"
          ? "text-frost"
          : "text-ink";
  return (
    <Card>
      <CardContent className="space-y-1 py-5">
        <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
          {label}
        </div>
        <div className={cn("font-display text-3xl leading-none", valueTone)}>
          {value}
        </div>
        {sub && <div className="text-xs text-inksoft">{sub}</div>}
      </CardContent>
    </Card>
  );
}

// ── Workflow step glyph ──────────────────────────────────────────────────────

function StepGlyph({ status }: { status: StepStatus }) {
  switch (status) {
    case "success":
      return (
        <span className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-ink bg-sage text-paper2">
          <Check aria-hidden="true" className="h-4 w-4" strokeWidth={3} />
        </span>
      );
    case "failed":
      return (
        <span className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-ink bg-terra text-paper2">
          <X aria-hidden="true" className="h-4 w-4" strokeWidth={3} />
        </span>
      );
    case "skipped":
      return (
        <span className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-line bg-paper3 text-inksoft">
          <Minus aria-hidden="true" className="h-4 w-4" strokeWidth={3} />
        </span>
      );
    case "running":
      return (
        <span className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-ink bg-paper2 text-frost">
          <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
        </span>
      );
    default:
      return (
        <span className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-line bg-paper2 text-inksoft">
          <Circle aria-hidden="true" className="h-3.5 w-3.5" />
        </span>
      );
  }
}

// ── Workflow timeline row ────────────────────────────────────────────────────

function StepRow({ step, last }: { step: WorkflowStep; last: boolean }) {
  return (
    <li className="relative flex gap-4 pb-5 last:pb-0">
      {/* vertical connector */}
      {!last && (
        <span
          aria-hidden="true"
          className="absolute left-[13px] top-7 h-[calc(100%-1.75rem)] w-0.5 bg-line"
        />
      )}
      <div className="relative z-10 shrink-0">
        <StepGlyph status={step.status} />
      </div>
      <div className="min-w-0 flex-1 pt-0.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-display text-base text-ink">
            {step.label || step.name}
          </span>
          <span className="font-mono text-xs text-inksoft">
            {fmtDuration(step.duration_ms)}
          </span>
          {step.attempts > 1 && (
            <span className="inline-flex items-center rounded-full border border-ochre/45 bg-ochre/15 px-2 py-0.5 font-mono text-[11px] font-semibold text-ochre">
              ×{step.attempts}
            </span>
          )}
        </div>
        {step.detail && (
          <p className="mt-0.5 text-sm leading-relaxed text-inksoft">
            {step.detail}
          </p>
        )}
      </div>
    </li>
  );
}

// ── Section heading ──────────────────────────────────────────────────────────

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="font-mono text-[11px] font-semibold uppercase tracking-widest text-inksoft">
      {children}
    </h3>
  );
}

// ── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({
  running,
  onRun,
}: {
  running: boolean;
  onRun: () => void;
}) {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="max-w-lg text-center">
        <div className="mb-6 flex justify-center">
          <ShapeBurst />
        </div>
        <h2 className="font-display text-3xl leading-tight text-ink">
          Let the agent fix it for you.
        </h2>
        <p className="mt-3 leading-relaxed text-inksoft">
          The autonomous agent analyzes your pipeline, reviews it dynamically,
          proposes &amp; applies fixes, then re-analyzes - reporting the
          readiness-score lift, the cost it saved, and the incidents it
          prevented.
        </p>
        <div className="mt-8 flex justify-center">
          <RunButton running={running} onClick={onRun} label="Run agent" />
        </div>
      </div>
    </div>
  );
}

// ── Business KPI hero ────────────────────────────────────────────────────────

function BusinessKpis({ run }: { run: AgentRun }) {
  const b = run.business_kpis;
  return (
    <section className="space-y-4">
      <SectionLabel>Business impact</SectionLabel>

      {/* Readiness score before → after */}
      <Card>
        <CardContent className="py-5">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-display text-2xl text-ink">
              Production readiness
            </span>
            {b.score_delta !== 0 && (
              <Badge tone={b.score_delta > 0 ? "sage" : "terra"}>
                <TrendingUp aria-hidden="true" className="h-3 w-3" />
                {b.score_delta > 0 ? "+" : ""}
                {b.score_delta} pts
              </Badge>
            )}
          </div>
          <div className="mt-5 grid items-center gap-4 sm:grid-cols-[1fr_auto_1fr]">
            <div className="space-y-1.5">
              <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
                Before
              </div>
              <div className="flex items-baseline gap-2">
                <span className="font-display text-4xl text-ink">
                  {Math.round(b.score_before)}
                </span>
                <span className="text-inksoft">/ 100</span>
              </div>
              <Meter
                value={b.score_before}
                max={100}
                tone={toneForScore(b.score_before)}
              />
            </div>
            <ArrowRight
              aria-hidden="true"
              className="mx-auto hidden h-6 w-6 text-inksoft sm:block"
            />
            <div className="space-y-1.5">
              <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
                After
              </div>
              <div className="flex items-baseline gap-2">
                <span className="font-display text-4xl text-sage">
                  {Math.round(b.score_after)}
                </span>
                <span className="text-inksoft">/ 100</span>
              </div>
              <Meter
                value={b.score_after}
                max={100}
                tone={toneForScore(b.score_after)}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Big stat cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatCard
          label="Monthly savings"
          value={fmtUSD(b.monthly_savings)}
          tone="sage"
          sub={`${Math.round(b.savings_pct)}% of warehouse spend`}
        />
        <StatCard
          label="Annual savings"
          value={fmtUSD(b.annual_savings)}
          tone="sage"
        />
        <StatCard
          label="Issues resolved"
          value={`${fmtInt(b.issues_resolved)} of ${fmtInt(b.issues_before)}`}
          sub={`${fmtInt(b.issues_after)} remaining`}
        />
        <StatCard
          label="Critical fixed"
          value={`${fmtInt(b.critical_before)} → ${fmtInt(b.critical_after)}`}
          tone={b.critical_after < b.critical_before ? "sage" : "ink"}
        />
        <StatCard
          label="Incidents prevented / mo"
          value={b.projected_incidents_prevented.toFixed(1)}
          tone="frost"
        />
        <StatCard
          label="Cost / mo"
          value={`${fmtUSD(b.monthly_cost_before)} → ${fmtUSD(
            b.monthly_cost_after,
          )}`}
          tone={b.monthly_cost_after < b.monthly_cost_before ? "sage" : "ink"}
        />
      </div>
    </section>
  );
}

// ── Durable workflow timeline ────────────────────────────────────────────────

function WorkflowTimeline({ run }: { run: AgentRun }) {
  return (
    <section className="space-y-4">
      <SectionLabel>Durable workflow</SectionLabel>
      <Card>
        <CardContent className="py-5">
          <ol>
            {run.steps.map((step, i) => (
              <StepRow
                key={step.name || i}
                step={step}
                last={i === run.steps.length - 1}
              />
            ))}
          </ol>
        </CardContent>
      </Card>
    </section>
  );
}

// ── Operational agent KPIs ───────────────────────────────────────────────────

function AgentKpis({ run }: { run: AgentRun }) {
  const k = run.agent_kpis;
  return (
    <section className="space-y-4">
      <SectionLabel>Agent telemetry</SectionLabel>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Steps"
          value={`${fmtInt(k.steps_succeeded)}/${fmtInt(k.steps_total)}`}
          tone={k.steps_failed > 0 ? "terra" : "sage"}
          sub={k.steps_failed > 0 ? `${fmtInt(k.steps_failed)} failed` : "all passed"}
        />
        <StatCard label="Retries" value={fmtInt(k.retries)} />
        <StatCard label="Total duration" value={fmtDuration(k.duration_ms)} />
        <StatCard
          label="Fixes"
          value={`${fmtInt(k.fixes_applied)} applied`}
          tone="frost"
          sub={`${fmtInt(k.fixes_proposed)} proposed`}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {k.used_llm ? (
          <Badge tone="sage">
            <Sparkles aria-hidden="true" className="h-3 w-3" />
            live LLM fixes
          </Badge>
        ) : (
          <Badge tone="ink">
            <ShieldCheck aria-hidden="true" className="h-3 w-3" />
            deterministic projection
          </Badge>
        )}
      </div>
    </section>
  );
}

// ── Main tab ─────────────────────────────────────────────────────────────────

export default function AgentTab() {
  const { code, params, connection } = useAnalysis();
  const [run, setRun] = useState<AgentRun | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const runningRef = useRef(false);

  async function handleRun() {
    if (runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    setError(null);
    try {
      const result = await runAgent({
        code,
        format: "auto",
        row_count: params.row_count,
        daily_runs: params.daily_runs,
        warehouse: params.warehouse,
        apply_fixes: true,
        connection: connection ?? undefined,
      });
      setRun(result);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      runningRef.current = false;
      setRunning(false);
    }
  }

  // No run yet and not running → editorial empty state.
  if (!run && !running) {
    return (
      <div className="h-full">
        {error && (
          <div className="mb-4 rounded-2xl border-2 border-terra bg-terra/10 p-4 shadow-block-sm">
            <p className="font-display text-lg text-terra">Agent run failed</p>
            <p className="mt-1 break-words text-sm leading-relaxed text-inksoft">
              {error}
            </p>
          </div>
        )}
        <EmptyState running={running} onRun={handleRun} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-inksoft">
            <Bot aria-hidden="true" className="h-3.5 w-3.5 text-frost" />
            Autonomous agent
          </span>
          <h2 className="max-w-xl font-display text-3xl leading-tight text-ink">
            Run the pipeline-doctor agent.
          </h2>
          {connection && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-frost/40 bg-frost/10 px-3 py-0.5 font-mono text-xs text-frost">
              <Database aria-hidden="true" className="h-3 w-3" />
              grounding in live {connection.kind}
            </span>
          )}
        </div>
        <RunButton
          running={running}
          onClick={handleRun}
          label={run ? "Re-run agent" : "Run agent"}
        />
      </header>

      {error && (
        <div className="rounded-2xl border-2 border-terra bg-terra/10 p-4 shadow-block-sm">
          <p className="font-display text-lg text-terra">Agent run failed</p>
          <p className="mt-1 break-words text-sm leading-relaxed text-inksoft">
            {error}
          </p>
        </div>
      )}

      {running && <RunningStatus />}

      {run && (
        <>
          {/* Deterministic summary */}
          <blockquote className="border-l-4 border-terra bg-paper3/60 py-3 pl-5 pr-4">
            <p className="font-display text-xl italic leading-relaxed text-ink">
              {run.summary}
            </p>
          </blockquote>

          <BusinessKpis run={run} />
          <WorkflowTimeline run={run} />
          <AgentKpis run={run} />
        </>
      )}
    </div>
  );
}
