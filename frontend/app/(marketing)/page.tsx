import type { Metadata } from "next";
import { Hero, ExploreHub, Stats } from "@/components/marketing";
import { FormatMarquee } from "@/components/format-marquee";

export const metadata: Metadata = {
  title: "Data Pipeline Copilot - know what your pipeline costs, breaks, and how to fix it",
  description:
    "Paste a SQL query, dbt model, Airflow DAG or Spark job and get cost, lineage, failure blast-radius, a production-readiness score, generated tests and a PII scan - from 85+ deterministic rules, running 100% locally. Not a chatbot.",
};

export default function HomePage() {
  return (
    <>
      <Hero />
      <FormatMarquee />
      <ExploreHub />
      <Stats />
    </>
  );
}
