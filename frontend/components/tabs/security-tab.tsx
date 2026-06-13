"use client";

/**
 * Security tab — a risk banner toned by the overall risk level, a PII column
 * table, the engine's narrative findings, and any security-category rule hits.
 */
import { Shield, ShieldAlert } from "lucide-react";
import { useAnalysis } from "@/lib/store";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SeverityBadge } from "@/components/ui/severity-badge";
import { Meter } from "@/components/ui/progress";
import type { RiskLevel } from "@/lib/types";

const RISK_HEADLINE: Record<RiskLevel, string> = {
  HIGH: "High risk — sensitive data flows unprotected",
  MEDIUM: "Medium risk — PII present",
  LOW: "Low risk — no obvious exposure",
};

function confidenceTone(confidence: number): "terra" | "ochre" | "frost" {
  if (confidence >= 0.8) return "terra";
  if (confidence >= 0.6) return "ochre";
  return "frost";
}

export default function SecurityTab() {
  const { report } = useAnalysis();

  if (!report) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-line bg-paper2 p-12 text-center shadow-block-sm">
        <p className="font-display text-2xl text-ink">No security scan yet</p>
        <p className="max-w-md text-inksoft">
          Analyze a pipeline to scan for PII columns, unprotected data flows,
          and security risks.
        </p>
      </div>
    );
  }

  const { security } = report;
  const { risk_level, pii_columns, findings } = security;

  const securityIssues = report.issues.filter(
    (i) => i.category === "security",
  );

  if (pii_columns.length === 0 && findings.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-sage bg-sage/10 p-12 text-center shadow-block-sm">
        <Shield className="h-10 w-10 text-sage" aria-hidden />
        <p className="font-display text-2xl text-ink">
          No PII or security risks detected
        </p>
        <p className="max-w-md text-inksoft">
          This pipeline shows no obvious sensitive-data exposure.
        </p>
        {securityIssues.length > 0 && (
          <RelatedRuleHits
            issues={securityIssues.map((i) => ({
              id: i.id,
              severity: i.severity,
              title: i.title,
              message: i.message,
            }))}
          />
        )}
      </div>
    );
  }

  // Banner tone keyed to risk level.
  const banner =
    risk_level === "HIGH"
      ? {
          wrap: "border-terra bg-terra/10",
          accent: "text-terra",
          icon: <ShieldAlert className="h-8 w-8 text-terra" aria-hidden />,
        }
      : risk_level === "MEDIUM"
        ? {
            wrap: "border-ochre bg-ochre/10",
            accent: "text-ochre",
            icon: <ShieldAlert className="h-8 w-8 text-ochre" aria-hidden />,
          }
        : {
            wrap: "border-sage bg-sage/10",
            accent: "text-sage",
            icon: <Shield className="h-8 w-8 text-sage" aria-hidden />,
          };

  return (
    <div className="flex flex-col gap-6">
      {/* ── Risk banner ──────────────────────────────────────────── */}
      <div
        className={`flex items-start gap-4 rounded-2xl border-2 p-6 shadow-block-sm ${banner.wrap}`}
      >
        <div className="shrink-0">{banner.icon}</div>
        <div>
          <p className={`font-display text-2xl ${banner.accent}`}>
            {RISK_HEADLINE[risk_level]}
          </p>
          <p className="mt-1 text-sm text-inksoft">
            {findings.length}{" "}
            {findings.length === 1 ? "finding" : "findings"} ·{" "}
            {pii_columns.length} PII{" "}
            {pii_columns.length === 1 ? "column" : "columns"}
          </p>
        </div>
      </div>

      {/* ── PII columns table ────────────────────────────────────── */}
      {pii_columns.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>PII columns</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b-2 border-line text-left">
                    <th className="py-2 pr-4 font-semibold text-inksoft">
                      Table
                    </th>
                    <th className="py-2 pr-4 font-semibold text-inksoft">
                      Column
                    </th>
                    <th className="py-2 pr-4 font-semibold text-inksoft">
                      Type
                    </th>
                    <th className="py-2 pr-4 font-semibold text-inksoft">
                      Confidence
                    </th>
                    <th className="py-2 font-semibold text-inksoft">
                      Recommendation
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {pii_columns.map((pii, idx) => (
                    <tr
                      key={`${pii.table}.${pii.column}-${idx}`}
                      className="border-b border-line last:border-b-0"
                    >
                      <td className="py-3 pr-4 font-mono text-ink">
                        {pii.table}
                      </td>
                      <td className="py-3 pr-4 font-mono text-ink">
                        {pii.column}
                      </td>
                      <td className="py-3 pr-4">
                        <Badge tone="frost">{pii.pii_type}</Badge>
                      </td>
                      <td className="w-40 py-3 pr-4">
                        <Meter
                          value={pii.confidence * 100}
                          max={100}
                          tone={confidenceTone(pii.confidence)}
                        />
                      </td>
                      <td className="py-3 text-inksoft">
                        {pii.recommendation}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Findings ─────────────────────────────────────────────── */}
      {findings.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Findings</CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="flex flex-col gap-3">
              {findings.map((finding, idx) => (
                <li key={idx} className="flex items-start gap-3">
                  <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 border-ink bg-paper3 font-mono text-xs text-ink">
                    {idx + 1}
                  </span>
                  <span className="text-sm text-ink">{finding}</span>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      )}

      {/* ── Related rule hits ────────────────────────────────────── */}
      {securityIssues.length > 0 && (
        <RelatedRuleHits
          issues={securityIssues.map((i) => ({
            id: i.id,
            severity: i.severity,
            title: i.title,
            message: i.message,
          }))}
        />
      )}
    </div>
  );
}

function RelatedRuleHits({
  issues,
}: {
  issues: {
    id: string;
    severity: "CRITICAL" | "WARNING" | "INFO";
    title: string;
    message: string;
  }[];
}) {
  return (
    <div className="flex w-full flex-col gap-3 text-left">
      <h3 className="font-display text-xl text-ink">Related rule hits</h3>
      <div className="flex flex-col gap-3">
        {issues.map((issue) => (
          <Card key={issue.id}>
            <CardContent>
              <div className="flex flex-wrap items-center gap-3">
                <SeverityBadge severity={issue.severity} />
                <p className="font-medium text-ink">{issue.title}</p>
              </div>
              <p className="mt-2 text-sm text-inksoft">{issue.message}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
