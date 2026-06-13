"use client";

/**
 * Production Impact Simulator — the signature feature.
 *
 * A control deck lets you dial the row count (logarithmic), runs/day, and
 * warehouse. Changes update the shared store params immediately and debounce a
 * call to the backend impact simulator (450ms), which re-projects per-issue
 * latency / cost / failure / incident impact plus a live cost figure.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { simulateImpact } from "@/lib/api";
import { useAnalysis } from "@/lib/store";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Meter } from "@/components/ui/progress";
import type {
  CostAnalysis,
  ImpactResult,
  Warehouse,
} from "@/lib/types";

const WAREHOUSE_OPTIONS: { value: Warehouse; label: string }[] = [
  { value: "snowflake", label: "Snowflake" },
  { value: "bigquery", label: "BigQuery" },
  { value: "redshift", label: "Redshift" },
  { value: "databricks", label: "Databricks" },
];

/** Round a number to two significant figures (keeps row counts tidy). */
function round2sig(n: number): number {
  if (n === 0) return 0;
  const mag = Math.floor(Math.log10(Math.abs(n)));
  const factor = 10 ** (mag - 1);
  return Math.round(n / factor) * factor;
}

/** Map a raw 0–100 slider position to a row count on a log scale (1e3..1e9). */
function rawToRows(raw: number): number {
  return round2sig(10 ** (3 + (raw * 6) / 100));
}

/** Invert rawToRows so we can seed the slider from a stored row count. */
function rowsToRaw(rows: number): number {
  const safe = Math.max(1e3, Math.min(1e9, rows));
  return Math.round(((Math.log10(safe) - 3) * 100) / 6);
}

const compactRows = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

function formatRows(raw: number): string {
  return `${compactRows.format(rawToRows(raw))} rows/run`;
}

export default function ImpactTab() {
  const { report, code, params, setParams } = useAnalysis();

  const [rawRows, setRawRows] = useState(0);
  const [impacts, setImpacts] = useState<ImpactResult[]>([]);
  const [cost, setCost] = useState<CostAnalysis | null>(null);
  const [inFlight, setInFlight] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);

  // Latest editor code, read at simulation time without making it an effect
  // dependency (so editing code does not retrigger the simulation; it only
  // serves as the stateless re-analysis fallback when the store misses).
  const codeRef = useRef(code);
  codeRef.current = code;

  // Seed local state from the report whenever a new analysis arrives. We adjust
  // state during render (the React-recommended pattern of tracking the previous
  // value in state) so the simulator never shows a frame of stale data and we
  // avoid cascading effect-driven re-renders.
  const [seededId, setSeededId] = useState<string | null>(null);
  if (report && seededId !== report.id) {
    setSeededId(report.id);
    setRawRows(rowsToRaw(report.params.row_count));
    setImpacts(report.impacts);
    setCost(report.cost_analysis);
    setApiError(null);
  }

  // Debounced simulation whenever the params change. The first effect run for a
  // freshly-seeded report is skipped (its impacts/cost already came from the
  // report), so we only hit the backend once the user actually dials a param.
  const seededForSim = useRef<string | null>(null);
  useEffect(() => {
    if (!report) return;
    if (seededForSim.current !== report.id) {
      seededForSim.current = report.id;
      return;
    }
    let cancelled = false;
    setInFlight(true);
    const handle = setTimeout(() => {
      simulateImpact({
        analysis_id: report.id,
        row_count: params.row_count,
        daily_runs: params.daily_runs,
        warehouse: params.warehouse,
        code: codeRef.current,
      })
        .then((res) => {
          if (cancelled) return;
          setImpacts(res.impacts);
          setCost(res.cost);
          setApiError(null);
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          setApiError(
            err instanceof Error ? err.message : "Simulation failed",
          );
        })
        .finally(() => {
          if (!cancelled) setInFlight(false);
        });
    }, 450);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [report?.id, params.row_count, params.daily_runs, params.warehouse]);

  const onRowsChange = useCallback(
    (raw: number) => {
      setRawRows(raw);
      setParams({ ...params, row_count: rawToRows(raw) });
    },
    [params, setParams],
  );

  const onRunsChange = useCallback(
    (runs: number) => {
      setParams({ ...params, daily_runs: runs });
    },
    [params, setParams],
  );

  const onWarehouseChange = useCallback(
    (warehouse: string) => {
      setParams({ ...params, warehouse: warehouse as Warehouse });
    },
    [params, setParams],
  );

  const totalCost = useMemo(
    () => impacts.reduce((sum, i) => sum + i.monthly_cost_increase_usd, 0),
    [impacts],
  );
  const totalIncidents = useMemo(
    () => impacts.reduce((sum, i) => sum + i.pagerduty_incidents_per_month, 0),
    [impacts],
  );

  if (!report) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-line bg-paper2 p-12 text-center shadow-block-sm">
        <p className="font-display text-2xl text-ink">
          No simulation yet
        </p>
        <p className="max-w-md text-inksoft">
          Analyze a pipeline to run the Production Impact Simulator and project
          cost, latency, and incident risk at scale.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {/* ── Control deck ─────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle>Production Impact Simulator</CardTitle>
          <p className="text-sm text-inksoft">
            Project the cost and reliability impact of every issue at production
            scale.
          </p>
        </CardHeader>
        <CardContent>
          <div className="grid gap-6 md:grid-cols-3">
            <Slider
              value={rawRows}
              onChange={onRowsChange}
              min={0}
              max={100}
              step={1}
              label="Row count / run"
              format={formatRows}
            />
            <Slider
              value={params.daily_runs}
              onChange={onRunsChange}
              min={1}
              max={96}
              step={1}
              label="Runs / day"
              format={(v) => `${v}× / day`}
            />
            <Select
              label="Warehouse"
              value={params.warehouse}
              onChange={onWarehouseChange}
              options={WAREHOUSE_OPTIONS}
            />
          </div>
          {apiError && (
            <p className="mt-4 rounded-xl border-2 border-terra bg-terra/10 px-4 py-2 text-sm text-terra">
              {apiError}
            </p>
          )}
        </CardContent>
      </Card>

      {/* ── Summary strip ────────────────────────────────────────── */}
      <div
        className={cnShimmer(
          "grid gap-4 transition-opacity duration-300 sm:grid-cols-3",
          inFlight,
        )}
      >
        <Card>
          <CardContent>
            <p className="text-xs font-medium uppercase tracking-wide text-inksoft">
              Extra cost / mo
            </p>
            <p className="mt-1 font-display text-3xl text-terra">
              {usd.format(totalCost)}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <p className="text-xs font-medium uppercase tracking-wide text-inksoft">
              Incidents / mo
            </p>
            <p className="mt-1 font-display text-3xl text-frost">
              {totalIncidents.toFixed(1)}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <p className="text-xs font-medium uppercase tracking-wide text-inksoft">
              Current monthly cost
            </p>
            <p className="mt-1 font-display text-3xl text-ink">
              {cost ? usd.format(cost.monthly_usd_current) : "—"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* ── Per-issue impact cards ───────────────────────────────── */}
      {impacts.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 rounded-2xl border-2 border-line bg-paper2 p-10 text-center shadow-block-sm">
          <p className="font-display text-xl text-ink">No measurable impact</p>
          <p className="text-inksoft">
            None of the detected issues carry a quantifiable production cost at
            these parameters.
          </p>
        </div>
      ) : (
        <div
          className={cnShimmer(
            "flex flex-col gap-4 transition-opacity duration-300",
            inFlight,
          )}
        >
          {impacts.map((imp) => (
            <ImpactCard
              key={imp.issue_id || imp.rule}
              imp={imp}
              title={
                report.issues.find((i) => i.id === imp.issue_id)?.title ??
                imp.rule
              }
              line={
                report.issues.find((i) => i.id === imp.issue_id)?.location
                  ?.line ?? null
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}

function cnShimmer(base: string, shimmering: boolean): string {
  return shimmering ? `${base} animate-pulse opacity-60` : base;
}

function ImpactCard({
  imp,
  title,
  line,
}: {
  imp: ImpactResult;
  title: string;
  line: number | null;
}) {
  const costCap = Math.max(1000, imp.monthly_cost_increase_usd);
  const failurePct = imp.failure_probability_per_run * 100;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-3">
          <span className="rounded-full border-2 border-line bg-paper3 px-3 py-0.5 font-mono text-xs text-inksoft">
            {imp.rule}
          </span>
          <CardTitle>{title}</CardTitle>
          {line != null && (
            <span className="rounded-full border-2 border-frost px-2 py-0.5 font-mono text-xs text-frost">
              L{line}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-4">
          <Meter
            value={imp.latency_increase_pct}
            max={2500}
            tone="terra"
            label={`Latency increase  +${Math.round(
              imp.latency_increase_pct,
            )}%`}
          />
          <Meter
            value={imp.monthly_cost_increase_usd}
            max={costCap}
            tone="ochre"
            label={`Monthly cost increase  ${usd.format(
              imp.monthly_cost_increase_usd,
            )}`}
          />
          <Meter
            value={failurePct}
            max={100}
            tone="plum"
            label={`Failure risk / run  ${failurePct.toFixed(1)}%`}
          />
          <Meter
            value={imp.pagerduty_incidents_per_month}
            max={60}
            tone="frost"
            label={`Incidents / month  ${imp.pagerduty_incidents_per_month.toFixed(
              1,
            )}`}
          />
        </div>
        <blockquote className="mt-5 border-l-4 border-terra pl-4 font-display text-lg italic text-ink">
          {imp.human_summary}
        </blockquote>
      </CardContent>
    </Card>
  );
}
