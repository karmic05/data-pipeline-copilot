import type { Metadata } from "next";
import { Features, PageHero, Privacy } from "@/components/marketing";

export const metadata: Metadata = {
  title: "Features - Data Pipeline Copilot",
  description:
    "Nine dimensions of pipeline analysis from one paste: plain-English explanation, 85+ issue rules, optimization diffs, warehouse-aware cost, column-level lineage, a production-readiness score, the Impact Simulator, generated tests, and a PII/security scan.",
};

export default function FeaturesPage() {
  return (
    <>
      <PageHero
        eyebrow="Features"
        title="One paste. Nine dimensions of analysis."
        sub="Datadog meets dbt Cloud meets an AI code reviewer - but structured, deterministic, and yours. Each report is built from a deterministic rule engine, never a guessing model."
      />
      <Features withHeading={false} />
      <Privacy />
    </>
  );
}
