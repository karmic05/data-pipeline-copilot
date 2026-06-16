import type { Metadata } from "next";
import { How, ImpactSpotlight, PageHero } from "@/components/marketing";

export const metadata: Metadata = {
  title: "How it works - Data Pipeline Copilot",
  description:
    "Paste your pipeline, let 85+ deterministic rules plus the cost and blast-radius engines run in seconds, then copy the fix. Auto-detects SQL, Airflow, dbt, Spark, Flink and Kafka - all locally.",
};

export default function HowItWorksPage() {
  return (
    <>
      <PageHero
        eyebrow="How it works"
        title="From paste to fix in three steps."
        sub="No project, no manifest, no warehouse connection. Paste arbitrary pipeline code and get a full structured report in seconds - then ask the AI to explain any finding."
      />
      <How withHeading={false} />
      <ImpactSpotlight />
    </>
  );
}
