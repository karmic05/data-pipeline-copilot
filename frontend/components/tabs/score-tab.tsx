"use client";

/**
 * Score tab: animated radial gauge + radar chart hero, the five scored
 * dimensions, and an editorial fix roadmap.
 */
import { useEffect, useRef, useState } from "react";
import {
  PolarAngleAxis,
  PolarGrid,
  Radar,
  RadarChart,
  ResponsiveContainer,
} from "recharts";
import { useAnalysis } from "@/lib/store";
import type { ProductionScore } from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Meter } from "@/components/ui/progress";

type Tone = "sage" | "ochre" | "terra";

const COLOR: Record<Tone, string> = {
  sage: "#6F8F6A",
  ochre: "#D99A2B",
  terra: "#D2402E",
};
const LINE_COLOR = "#D8CCB8";
const FROST = "#1F8FB8";
const INKSOFT = "#6B5D4F";

/** Score band → design tone. */
function toneForScore(score: number): Tone {
  if (score >= 80) return "sage";
  if (score >= 50) return "ochre";
  return "terra";
}

const DIMENSIONS: { key: keyof ProductionScore; label: string; weight: string }[] =
  [
    { key: "efficiency", label: "Efficiency", weight: "25% of total" },
    { key: "reliability", label: "Reliability", weight: "25% of total" },
    { key: "observability", label: "Observability", weight: "20% of total" },
    { key: "maintainability", label: "Maintainability", weight: "15% of total" },
    { key: "security", label: "Security", weight: "15% of total" },
  ];

const GAUGE_SIZE = 240;
const STROKE = 18;
const RADIUS = (GAUGE_SIZE - STROKE) / 2;
const CIRC = 2 * Math.PI * RADIUS;

function RadialGauge({ value }: { value: number }) {
  const [animated, setAnimated] = useState(0);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const start = performance.now();
    const from = 0;
    const to = Math.max(0, Math.min(100, value));
    const duration = 900;

    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - t, 3);
      setAnimated(from + (to - from) * eased);
      if (t < 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);

    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [value]);

  const tone = toneForScore(value);
  const offset = CIRC * (1 - animated / 100);
  const center = GAUGE_SIZE / 2;

  return (
    <div className="relative" style={{ width: GAUGE_SIZE, height: GAUGE_SIZE }}>
      <svg
        width={GAUGE_SIZE}
        height={GAUGE_SIZE}
        viewBox={`0 0 ${GAUGE_SIZE} ${GAUGE_SIZE}`}
        role="img"
        aria-label={`Production readiness score ${Math.round(value)} of 100`}
      >
        <circle
          cx={center}
          cy={center}
          r={RADIUS}
          fill="none"
          stroke={LINE_COLOR}
          strokeWidth={STROKE}
        />
        <circle
          cx={center}
          cy={center}
          r={RADIUS}
          fill="none"
          stroke={COLOR[tone]}
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={CIRC}
          strokeDashoffset={offset}
          transform={`rotate(-90 ${center} ${center})`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="font-display text-6xl leading-none text-ink">
          {Math.round(animated)}
        </span>
        <span className="mt-2 text-xs uppercase tracking-wide text-inksoft">
          production readiness
        </span>
      </div>
    </div>
  );
}

export default function ScoreTab() {
  const { report } = useAnalysis();

  if (!report) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="font-display text-xl text-inksoft">
          Run an analysis to see the production score.
        </p>
      </div>
    );
  }

  const score = report.production_score;
  const radarData = DIMENSIONS.map((d) => ({
    dimension: d.label,
    value: score[d.key] as number,
  }));

  return (
    <div className="space-y-8">
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardContent className="flex items-center justify-center py-8">
            <RadialGauge value={score.total} />
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <p className="mb-2 text-xs uppercase tracking-wide text-inksoft">
              Dimension profile
            </p>
            <div style={{ width: "100%", height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <RadarChart data={radarData} outerRadius="78%">
                  <PolarGrid stroke={LINE_COLOR} />
                  <PolarAngleAxis
                    dataKey="dimension"
                    tick={{ fill: INKSOFT, fontSize: 12 }}
                  />
                  <Radar
                    dataKey="value"
                    stroke={FROST}
                    fill={FROST}
                    fillOpacity={0.25}
                  />
                </RadarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        {DIMENSIONS.map((d) => {
          const v = score[d.key] as number;
          const tone = toneForScore(v);
          return (
            <Card key={d.key}>
              <CardContent>
                <p className="text-xs uppercase tracking-wide text-inksoft">
                  {d.label}
                </p>
                <div className="my-3">
                  <Meter value={v} max={100} tone={tone} />
                </div>
                <div className="flex items-end justify-between">
                  <span className="font-display text-3xl leading-none text-ink">
                    {Math.round(v)}
                  </span>
                  <span className="text-xs text-inksoft">{d.weight}</span>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <section>
        <h3 className="mb-4 font-display text-2xl text-ink">Fix roadmap</h3>
        {score.roadmap.length === 0 ? (
          <Card>
            <CardContent>
              <p className="text-inksoft leading-relaxed">
                Nothing on the roadmap — this pipeline is already in good shape.
                Ship it.
              </p>
            </CardContent>
          </Card>
        ) : (
          <ol className="space-y-3">
            {score.roadmap.map((item, i) => (
              <li key={`${item.dimension}-${i}`}>
                <Card>
                  <CardContent className="flex items-start gap-4">
                    <span className="font-display text-4xl leading-none text-frost">
                      {i + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="text-ink leading-relaxed">{item.action}</p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <Badge tone="frost">{item.dimension}</Badge>
                        <span className="inline-flex items-center rounded-full border-2 border-ink bg-sage px-3 py-0.5 text-xs font-semibold text-paper2">
                          +{item.estimated_gain} pts
                        </span>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
