"use client";

/**
 * Observability tab - surfaces the auto-generated test suite (dbt + Great
 * Expectations), the framework-grouped test chips, and any coverage gaps the
 * engine flagged.
 */
import { AlertTriangle } from "lucide-react";
import { useAnalysis } from "@/lib/store";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { CodeBlock } from "@/components/ui/code-block";
import type { GeneratedTest } from "@/lib/types";

const FRAMEWORK_LABELS: Record<GeneratedTest["framework"], string> = {
  dbt: "dbt",
  great_expectations: "Great Expectations",
};

const FRAMEWORK_ORDER: GeneratedTest["framework"][] = [
  "dbt",
  "great_expectations",
];

export default function ObservabilityTab() {
  const { report } = useAnalysis();

  if (!report) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-line bg-paper2 p-12 text-center shadow-block-sm">
        <p className="font-display text-2xl text-ink">No test suite yet</p>
        <p className="max-w-md text-inksoft">
          Analyze a pipeline to generate dbt and Great Expectations tests and
          surface coverage gaps.
        </p>
      </div>
    );
  }

  const { generated_tests } = report;
  const { tests, coverage_gaps, dbt_yaml, great_expectations_json } =
    generated_tests;

  if (tests.length === 0 && coverage_gaps.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-line bg-paper2 p-12 text-center shadow-block-sm">
        <p className="font-display text-2xl text-ink">
          Nothing to test here
        </p>
        <p className="max-w-md text-inksoft">
          No tests were generated and no coverage gaps were detected for this
          pipeline.
        </p>
      </div>
    );
  }

  // Verdict chip: sage when gaps are few, ochre when several.
  const gapsAreManageable = coverage_gaps.length <= 2;

  const groups = FRAMEWORK_ORDER.map((framework) => ({
    framework,
    label: FRAMEWORK_LABELS[framework],
    items: tests.filter((t) => t.framework === framework),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="flex flex-col gap-6">
      {/* ── Header ───────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-display text-2xl text-ink">
          {tests.length} {tests.length === 1 ? "test" : "tests"} generated
        </h2>
        <Badge tone={gapsAreManageable ? "sage" : "ochre"}>
          {gapsAreManageable
            ? "Solid coverage"
            : `${coverage_gaps.length} coverage gaps`}
        </Badge>
      </div>

      {/* ── Coverage gaps ────────────────────────────────────────── */}
      {coverage_gaps.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Coverage gaps</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-2">
              {coverage_gaps.map((gap, idx) => (
                <li
                  key={idx}
                  className="flex items-start gap-3 rounded-xl border-2 border-ochre bg-ochre/10 px-4 py-3"
                >
                  <AlertTriangle
                    className="mt-0.5 h-5 w-5 shrink-0 text-ochre"
                    aria-hidden
                  />
                  <span className="text-sm text-ink">{gap}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* ── Generated test chips (grouped by framework) ──────────── */}
      {groups.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Generated checks</CardTitle>
            <p className="text-sm text-inksoft">
              Hover a check to read what it asserts.
            </p>
          </CardHeader>
          <CardContent>
            <div className="flex flex-col gap-5">
              {groups.map((group) => (
                <div key={group.framework}>
                  <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-inksoft">
                    {group.label}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {group.items.map((test, idx) => (
                      <Tooltip
                        key={`${test.name}-${test.target}-${idx}`}
                        content={test.description}
                      >
                        <span className="inline-flex cursor-help rounded-full border-2 border-line bg-paper3 px-3 py-1 font-mono text-xs text-ink">
                          {test.name} &rarr; {test.target}
                        </span>
                      </Tooltip>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Generated suite source ───────────────────────────────── */}
      <div className="grid gap-4 xl:grid-cols-2">
        <CodeBlock
          title="schema.yml - dbt tests"
          code={dbt_yaml}
          language="yaml"
        />
        <CodeBlock
          title="expectations.json - Great Expectations"
          code={great_expectations_json}
          language="json"
        />
      </div>
    </div>
  );
}
