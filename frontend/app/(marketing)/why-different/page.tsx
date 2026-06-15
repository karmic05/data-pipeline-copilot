import type { Metadata } from "next";
import { Determinism, Different, PageHero } from "@/components/marketing";

export const metadata: Metadata = {
  title: "Why it's different — Data Pipeline Copilot",
  description:
    "Not a runtime monitor, not dbt-only, not a guessing chatbot, not a cloud SaaS. A static, code-first, deterministic, local analyzer that catches the expensive mistakes before deploy.",
};

export default function WhyDifferentPage() {
  return (
    <>
      <PageHero
        eyebrow="Why it's different"
        title="A different shape than everything else."
        sub="Most data tooling is a connected SaaS that watches data at runtime. Copilot sits on the other side of the deploy line — static, deterministic, multi-framework, and local."
      />
      <Different withHeading={false} />
      <Determinism />
    </>
  );
}
