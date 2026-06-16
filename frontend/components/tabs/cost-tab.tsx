"use client";

/**
 * Cost tab — warehouse cost projections rendered with recharts. Shows current vs
 * optimized monthly spend, a savings/risk summary, a 30/90/365-day bar chart, and
 * the deterministic reasoning behind the estimate. Reads from useAnalysis().
 */
import {
  BarChart,
  Bar,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { DollarSign, ArrowRight, Database } from "lucide-react";
import { Card, CardContent, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useAnalysis } from "@/lib/store";
import type { CostAnalysis, RiskLevel } from "@/lib/types";

// ── Currency helpers ─────────────────────────────────────────────────────────

const fmtUSD = (v: number): string =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(v);

const fmtCompact = (v: number): string =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(v);

/** Pretty-print a byte count, e.g. 1_320_000_000_000 -> "1.2 TB". */
function fmtBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

// ── Toning ───────────────────────────────────────────────────────────────────

const RISK_TONE: Record<RiskLevel, "terra" | "ochre" | "sage"> = {
  HIGH: "terra",
  MEDIUM: "ochre",
  LOW: "sage",
};

function ProviderChips({ cost }: { cost: CostAnalysis }) {
  const chips: string[] = [cost.provider];
  if (cost.provider === "bigquery") {
    chips.push(`${fmtBytes(cost.bytes_billed)} billed`);
  } else {
    chips.push(`${cost.credits_per_run.toFixed(2)} credits / run`);
  }
  return (
    <div className="flex flex-wrap gap-2">
      {chips.map((c) => (
        <span
          key={c}
          className="rounded-full border-2 border-line bg-paper2 px-3 py-1 font-mono text-xs text-inksoft"
        >
          {c}
        </span>
      ))}
    </div>
  );
}

// ── Main tab ─────────────────────────────────────────────────────────────────

export default function CostTab() {
  const { report } = useAnalysis();

  if (!report) {
    return (
      <Card className="mx-auto mt-8 max-w-md text-center">
        <CardContent className="space-y-3 py-10">
          <DollarSign className="mx-auto h-10 w-10 text-sage" />
          <CardTitle className="text-xl">No cost estimate yet</CardTitle>
          <p className="text-inksoft">
            Analyze a pipeline to project monthly warehouse spend and see how
            much the optimizations would save.
          </p>
        </CardContent>
      </Card>
    );
  }

  const cost = report.cost_analysis;
  const { projections } = cost;
  const highRisk = cost.risk_level === "HIGH";

  // Live-grounding notes surfaced when the analysis was run against a real DB
  // connection (real schemas + profiled / BigQuery-dry-run cost).
  const liveNotes = report.parser_warnings
    .filter((w) => w.startsWith("[live]"))
    .map((w) => w.replace(/^\[live\]\s*/, ""));
  const grounded = liveNotes.length > 0;

  const chartData = [
    {
      name: "30 days",
      current: projections.d30_current,
      optimized: projections.d30_optimized,
    },
    {
      name: "90 days",
      current: projections.d90_current,
      optimized: projections.d90_optimized,
    },
    {
      name: "365 days",
      current: projections.d365_current,
      optimized: projections.d365_optimized,
    },
  ];

  return (
    <div className="space-y-6">
      {/* Stat row */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="space-y-1 py-5">
            <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
              Current / mo
            </div>
            <div
              className={`font-display text-4xl ${
                highRisk ? "text-terra" : "text-ink"
              }`}
            >
              {fmtUSD(cost.monthly_usd_current)}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="space-y-1 py-5">
            <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
              Optimized / mo
            </div>
            <div className="font-display text-4xl text-sage">
              {fmtUSD(cost.monthly_usd_optimized)}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="space-y-2 py-5">
            <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
              Savings
            </div>
            <div className="flex items-center gap-2">
              <span className="font-display text-4xl text-ink">
                {Math.round(cost.savings_pct)}%
              </span>
              <Badge tone="sage">save</Badge>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="space-y-2 py-5">
            <div className="font-mono text-[11px] uppercase tracking-wider text-inksoft">
              Risk
            </div>
            <div>
              <Badge tone={RISK_TONE[cost.risk_level]}>
                {cost.risk_level}
              </Badge>
            </div>
          </CardContent>
        </Card>
      </div>

      <ProviderChips cost={cost} />

      {grounded && (
        <div className="flex flex-wrap items-start gap-3 rounded-2xl border-2 border-frost/50 bg-frost/10 p-4 shadow-block-sm">
          <span className="flex shrink-0 items-center gap-2 font-display text-lg text-frost">
            <Database aria-hidden="true" className="h-4 w-4" />
            Grounded in live data
          </span>
          <ul className="min-w-0 flex-1 space-y-1 text-sm leading-relaxed text-ink">
            {liveNotes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Chart */}
      <Card>
        <CardContent className="py-5">
          <CardTitle className="mb-4 text-xl">
            Projected spend — current vs optimized
          </CardTitle>
          <ResponsiveContainer width="100%" height={290}>
            <BarChart
              data={chartData}
              margin={{ top: 8, right: 8, bottom: 0, left: 8 }}
            >
              <CartesianGrid stroke="#D8CCB8" strokeDasharray="4 4" />
              <XAxis
                dataKey="name"
                tick={{
                  fill: "#6B5D4F",
                  fontFamily: "var(--font-mono), monospace",
                  fontSize: 11,
                }}
                stroke="#D8CCB8"
              />
              <YAxis
                tickFormatter={fmtCompact}
                tick={{
                  fill: "#6B5D4F",
                  fontFamily: "var(--font-mono), monospace",
                  fontSize: 11,
                }}
                stroke="#D8CCB8"
              />
              <Tooltip
                cursor={{ fill: "rgba(46, 38, 32, 0.06)" }}
                contentStyle={{
                  background: "#FDFAF2",
                  border: "2px solid #2E2620",
                  borderRadius: 12,
                }}
                labelStyle={{ color: "#2E2620", fontWeight: 600 }}
                formatter={(value) => fmtUSD(Number(value) || 0)}
              />
              <Legend
                wrapperStyle={{
                  fontFamily: "var(--font-mono), monospace",
                  fontSize: 12,
                }}
              />
              <Bar
                dataKey="current"
                name="Current"
                fill="#D2402E"
                radius={[6, 6, 0, 0]}
              />
              <Bar
                dataKey="optimized"
                name="Optimized"
                fill="#6F8F6A"
                radius={[6, 6, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      {/* Reasoning */}
      <Card>
        <CardContent className="py-5">
          <h3 className="font-display text-2xl text-ink">
            How we got these numbers
          </h3>
          {cost.reasoning.length === 0 ? (
            <p className="mt-3 text-inksoft">
              No reasoning was recorded for this estimate.
            </p>
          ) : (
            <ol className="mt-4 space-y-3">
              {cost.reasoning.map((line, i) => (
                <li key={i} className="flex gap-3 leading-relaxed">
                  <span
                    className={`font-display text-2xl leading-none ${
                      line.startsWith("Live ") ? "text-frost" : "text-terra"
                    }`}
                  >
                    {i + 1}.
                  </span>
                  <span
                    className={`pt-0.5 ${
                      line.startsWith("Live ") ? "font-medium text-frost" : "text-ink"
                    }`}
                  >
                    {line}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      {/* Footnote */}
      <Card className="bg-paper3">
        <CardContent className="py-4 text-sm text-inksoft">
          Estimates use warehouse list prices and IR-derived heuristics —
          calibrate with your real query history.
        </CardContent>
      </Card>

      {/* Hint */}
      <p className="flex items-center gap-1.5 text-sm text-inksoft">
        Want different volumes? Head to the
        <span className="inline-flex items-center gap-1 font-medium text-frost">
          Impact tab <ArrowRight className="h-3.5 w-3.5" />
        </span>
        to re-scale row counts and run frequency.
      </p>
    </div>
  );
}
