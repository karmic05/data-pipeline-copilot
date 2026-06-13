/**
 * Marketing landing page (/). Server-rendered, built entirely from the shared
 * design tokens (paper/ink/terra/frost/ochre/sage/plum), Fraunces display type,
 * Memphis accents and offset block shadows — so it reads as one continuous
 * design with the analyzer app at /app. The primary CTA routes to /app.
 */
import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import {
  Activity,
  ArrowRight,
  Bug,
  Check,
  DollarSign,
  FileText,
  FlaskConical,
  Gauge,
  Lock,
  ShieldCheck,
  Workflow,
  Wrench,
  Zap,
} from "lucide-react";
import { Squiggle, DotGrid } from "@/components/memphis";

const APP_HREF = "/app";
const GITHUB_HREF = "https://github.com/karmic05/data-pipeline-copilot";

export const metadata: Metadata = {
  title: "Data Pipeline Copilot — know what your pipeline costs, breaks, and how to fix it",
  description:
    "Paste a SQL query, dbt model, Airflow DAG or Spark job and get cost, lineage, failure blast-radius, a production-readiness score, generated tests and a PII scan — from 85+ deterministic rules, running 100% locally. Not a chatbot.",
};

/* ── Small shared pieces ─────────────────────────────────────────────────── */

function GithubMark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.21 11.39.6.11.82-.26.82-.58 0-.29-.01-1.04-.02-2.05-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.21.09 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.5.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.63-5.49 5.92.43.37.81 1.1.81 2.22 0 1.6-.01 2.89-.01 3.28 0 .32.22.7.83.58A12.01 12.01 0 0 0 24 12.5C24 5.87 18.63.5 12 .5z" />
    </svg>
  );
}

function Logo({ className = "" }: { className?: string }) {
  return (
    <span className={`relative inline-block h-7 w-7 shrink-0 ${className}`} aria-hidden="true">
      <span className="absolute left-0 top-1 h-4 w-4 rotate-12 rounded-[4px] border-2 border-ink bg-terra" />
      <span className="absolute left-2 top-0 h-4 w-4 -rotate-6 rounded-[4px] border-2 border-ink bg-frost" />
      <span className="absolute left-1 top-3 h-4 w-4 rotate-[20deg] rounded-[4px] border-2 border-ink bg-ochre" />
    </span>
  );
}

const CTA_BASE =
  "inline-flex select-none items-center justify-center gap-2 rounded-xl border-2 border-ink font-medium transition active:translate-x-[3px] active:translate-y-[3px] active:shadow-none";
const CTA_PRIMARY = "bg-terra text-paper2 shadow-block hover:brightness-105";
const CTA_OUTLINE = "bg-paper2 text-ink shadow-block hover:bg-paper3";

function CtaLink({
  href,
  variant = "primary",
  size = "lg",
  external = false,
  className = "",
  children,
}: {
  href: string;
  variant?: "primary" | "outline";
  size?: "md" | "lg";
  external?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const sizing = size === "lg" ? "h-12 px-6 text-base" : "h-10 px-4 text-sm";
  const cls = `${CTA_BASE} ${variant === "primary" ? CTA_PRIMARY : CTA_OUTLINE} ${sizing} ${className}`;
  if (external) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className={cls}>
        {children}
      </a>
    );
  }
  return (
    <Link href={href} className={cls}>
      {children}
    </Link>
  );
}

function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-inksoft">
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-terra" />
      {children}
    </span>
  );
}

/* ── Navigation ──────────────────────────────────────────────────────────── */

function Nav() {
  return (
    <header className="sticky top-0 z-50 border-b-2 border-ink bg-paper2/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-4 px-5 py-3">
        <Link href="/" className="flex items-center gap-3" aria-label="Pipeline Copilot home">
          <Logo />
          <span className="flex flex-col leading-none">
            <span className="text-[10px] uppercase tracking-widest text-inksoft">data intelligence</span>
            <span className="font-display text-xl text-ink">Pipeline Copilot</span>
          </span>
        </Link>
        <nav className="ml-auto hidden items-center gap-7 text-sm font-medium text-inksoft md:flex">
          <a href="#features" className="transition-colors hover:text-ink">Features</a>
          <a href="#how" className="transition-colors hover:text-ink">How it works</a>
          <a href="#different" className="transition-colors hover:text-ink">Why different</a>
          <a href={GITHUB_HREF} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 transition-colors hover:text-ink">
            <GithubMark className="h-4 w-4" /> GitHub
          </a>
        </nav>
        <CtaLink href={APP_HREF} size="md" className="ml-auto md:ml-0">
          Analyze Pipeline
          <ArrowRight className="h-4 w-4" />
        </CtaLink>
      </div>
    </header>
  );
}

/* ── Hero ────────────────────────────────────────────────────────────────── */

function Hero() {
  return (
    <section className="relative overflow-hidden">
      <div className="pointer-events-none absolute inset-0 memphis-dots opacity-50" aria-hidden="true" />
      <DotGrid className="pointer-events-none absolute right-8 top-28 hidden text-line/70 lg:block" rows={5} cols={5} aria-hidden="true" />
      <div className="relative mx-auto grid max-w-6xl items-center gap-12 px-5 py-20 lg:grid-cols-[1.1fr_0.9fr] lg:py-28">
        <div>
          <Eyebrow>Data intelligence · not a chatbot</Eyebrow>
          <h1 className="mt-5 font-display text-4xl leading-[1.05] tracking-tight text-ink sm:text-5xl lg:text-6xl">
            Know what your pipeline{" "}
            <span className="text-terra">costs</span>, what{" "}
            <span className="text-terra">breaks</span>, and how to{" "}
            <span className="text-sage">fix it</span> — before it ships.
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-relaxed text-inksoft">
            Paste a SQL query, dbt model, Airflow DAG or Spark job. In seconds, Pipeline Copilot runs{" "}
            <strong className="font-semibold text-ink">85+ deterministic checks</strong> and hands you the dollar cost,
            the failure blast-radius, the data lineage, and the exact fix — running{" "}
            <strong className="font-semibold text-ink">100% locally</strong>, no warehouse connection, no account.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <CtaLink href={APP_HREF}>
              Analyze Pipeline
              <ArrowRight className="h-4 w-4" />
            </CtaLink>
            <CtaLink href={GITHUB_HREF} variant="outline" external>
              <GithubMark className="h-4 w-4" />
              View on GitHub
            </CtaLink>
          </div>
          <p className="mt-6 font-mono text-xs text-inksoft">
            8 frameworks · 85+ rules · runs offline · your code never leaves your machine
          </p>
        </div>
        <HeroCard />
      </div>
      <Squiggle className="text-line" />
    </section>
  );
}

/** A static mock of an analysis result — echoes the real app for continuity. */
function HeroCard() {
  return (
    <div className="relative mx-auto w-full max-w-md rotate-1 rounded-2xl border-2 border-ink bg-paper2 p-6 shadow-block">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-widest text-inksoft">analysis report</span>
        <span className="rounded-full border border-line bg-paper3 px-2 py-0.5 font-mono text-[10px] text-ink">sql · snowflake</span>
      </div>

      <div className="mt-5 flex items-center gap-5">
        <svg viewBox="0 0 120 120" className="h-24 w-24 shrink-0" aria-hidden="true">
          <circle cx="60" cy="60" r="52" fill="none" stroke="var(--color-paper3)" strokeWidth="12" />
          <circle
            cx="60" cy="60" r="52" fill="none"
            stroke="var(--color-ochre)" strokeWidth="12" strokeLinecap="round"
            strokeDasharray="258 327" transform="rotate(-90 60 60)"
          />
          <text x="60" y="68" textAnchor="middle" className="fill-ink font-display" style={{ fontSize: 30 }}>79</text>
        </svg>
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-widest text-inksoft">production readiness</p>
          <p className="mt-1 text-sm text-inksoft">11 issues found · <span className="font-semibold text-terra">HIGH</span> cost risk</p>
          <p className="mt-2 font-display text-2xl text-ink">
            $14,580<span className="text-base text-inksoft">/mo</span>
            <span className="mx-2 text-inksoft">→</span>
            <span className="text-sage">$243</span>
          </p>
        </div>
      </div>

      <div className="mt-5 space-y-2">
        {[
          { sev: "CRITICAL", tone: "bg-terra text-paper2", title: "Cartesian join", line: "L14" },
          { sev: "CRITICAL", tone: "bg-terra text-paper2", title: "SELECT * in production", line: "L3" },
          { sev: "WARNING", tone: "bg-ochre text-ink", title: "Missing partition filter", line: "L9" },
        ].map((i) => (
          <div key={i.title} className="flex items-center gap-3 rounded-xl border border-line bg-paper px-3 py-2">
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${i.tone}`}>{i.sev}</span>
            <span className="text-sm text-ink">{i.title}</span>
            <span className="ml-auto rounded-full border border-frost/50 px-2 py-0.5 font-mono text-[10px] text-frost">{i.line}</span>
          </div>
        ))}
      </div>

      <div className="mt-4 flex items-center gap-2 rounded-xl border-2 border-sage/40 bg-sage/10 px-3 py-2 text-sm text-sage">
        <Check className="h-4 w-4" /> Fix diff ready · 98% projected savings
      </div>
    </div>
  );
}

/* ── Formats strip ───────────────────────────────────────────────────────── */

const FORMATS = [
  "SQL", "Snowflake", "BigQuery", "Redshift", "Airflow", "dbt",
  "Spark", "Flink", "Kafka", "Prefect", "Dagster", "Great Expectations",
];

function Formats() {
  return (
    <section className="border-y-2 border-ink bg-paper2">
      <div className="mx-auto max-w-6xl px-5 py-10">
        <p className="text-center text-xs font-semibold uppercase tracking-widest text-inksoft">
          Understands your whole stack — auto-detected from the code
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          {FORMATS.map((f) => (
            <span key={f} className="rounded-full border-2 border-ink bg-paper px-4 py-1.5 font-mono text-sm text-ink shadow-block-sm">
              {f}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ── Feature grid (the 9 dimensions) ─────────────────────────────────────── */

const FEATURES = [
  { icon: FileText, title: "Plain-English explanation", body: "Reads your pipeline back to you in plain language — every table, join and transform, with no guesswork." },
  { icon: Bug, title: "85+ issue rules", body: "A deterministic engine flags cartesian joins, SELECT *, missing partition filters, no-retry DAGs, missing watermarks and 80 more — each tied to an exact line." },
  { icon: Wrench, title: "Optimization diffs", body: "Every fix arrives as a copy-paste unified diff. Not 'consider refactoring' — the actual corrected code." },
  { icon: DollarSign, title: "Warehouse-aware cost", body: "Real dollar math for Snowflake, BigQuery, Redshift and Databricks: cost now, cost fixed, and the 30 / 90 / 365-day projection." },
  { icon: Workflow, title: "Column-level lineage", body: "An interactive graph of how every column flows source-to-output. Export to Mermaid or OpenLineage." },
  { icon: Gauge, title: "Production readiness score", body: "A 0–100 grade across efficiency, reliability, observability, maintainability and security — with a prioritized fix roadmap." },
  { icon: Activity, title: "Production Impact Simulator", body: "Dial up the scale and watch each issue's blast radius: added latency, extra $/month, failure probability and incidents per month." },
  { icon: FlaskConical, title: "Auto-generated tests", body: "Ships the data-quality tests you're missing as ready-to-apply dbt schema tests and Great Expectations suites." },
  { icon: ShieldCheck, title: "PII & security scan", body: "Detects PII columns, hardcoded secrets and unmasked data flowing to outputs — before it becomes a compliance incident." },
];

function Features() {
  return (
    <section id="features" className="mx-auto max-w-6xl px-5 py-20 lg:py-28">
      <div className="max-w-2xl">
        <Eyebrow>Nine kinds of intelligence</Eyebrow>
        <h2 className="mt-4 font-display text-3xl tracking-tight text-ink sm:text-4xl">
          One paste. Nine dimensions of analysis.
        </h2>
        <p className="mt-4 text-lg leading-relaxed text-inksoft">
          Datadog meets dbt Cloud meets an AI code reviewer — but structured, deterministic, and yours.
        </p>
      </div>
      <div className="mt-12 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map(({ icon: Icon, title, body }) => (
          <div
            key={title}
            className="group rounded-2xl border-2 border-ink bg-paper2 p-6 shadow-block-sm transition-transform hover:-translate-y-1 hover:shadow-block"
          >
            <span className="inline-flex h-11 w-11 items-center justify-center rounded-xl border-2 border-ink bg-paper text-terra">
              <Icon className="h-5 w-5" />
            </span>
            <h3 className="mt-4 font-display text-xl text-ink">{title}</h3>
            <p className="mt-2 text-sm leading-relaxed text-inksoft">{body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── "Not a chatbot" determinism ─────────────────────────────────────────── */

function Determinism() {
  return (
    <section className="border-y-2 border-ink bg-ink text-paper">
      <div className="mx-auto grid max-w-6xl items-center gap-10 px-5 py-20 lg:grid-cols-2 lg:py-24">
        <div>
          <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-paper3">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-terra" />
            The trust model
          </span>
          <h2 className="mt-4 font-display text-3xl tracking-tight sm:text-4xl">
            This is not a chatbot.
          </h2>
          <p className="mt-5 text-lg leading-relaxed text-paper3">
            The findings come from a deterministic 85-rule engine — the same code always gives the same answer. The
            LLM only ever <em>explains</em> a finding in plain English; it never invents one. And it never sees your
            raw code — only a structured intermediate representation.
          </p>
          <p className="mt-4 text-lg leading-relaxed text-paper3">
            No hallucinated table names. No made-up problems. No code leaving your machine.
          </p>
        </div>
        <div className="grid gap-4">
          {[
            { k: "Deterministic", v: "Rules are the source of truth. The AI is a narrator, not an oracle." },
            { k: "Grounded", v: "Every output is tied to a real IR node — an actual line of your pipeline." },
            { k: "Private", v: "Runs locally. The model sees structured metadata, never your source." },
          ].map((row) => (
            <div key={row.k} className="rounded-2xl border-2 border-paper3/30 bg-paper/5 p-5">
              <p className="font-display text-xl text-paper">{row.k}</p>
              <p className="mt-1 text-paper3">{row.v}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ── Signature: Impact Simulator ─────────────────────────────────────────── */

const IMPACT_BARS = [
  { label: "Latency increase", value: "+2,400%", width: "100%", tone: "bg-terra" },
  { label: "Monthly cost increase", value: "+$4,200", width: "78%", tone: "bg-ochre" },
  { label: "Failure risk / run", value: "35%", width: "35%", tone: "bg-plum" },
  { label: "Incidents / month", value: "~21", width: "60%", tone: "bg-frost" },
];

function ImpactSpotlight() {
  return (
    <section className="mx-auto max-w-6xl px-5 py-20 lg:py-28">
      <div className="grid items-center gap-12 lg:grid-cols-2">
        <div>
          <Eyebrow>Signature feature</Eyebrow>
          <h2 className="mt-4 font-display text-3xl tracking-tight text-ink sm:text-4xl">
            See the blast radius before it pages you.
          </h2>
          <p className="mt-5 text-lg leading-relaxed text-inksoft">
            The Production Impact Simulator turns every issue into a real-world forecast. Dial the data volume, the
            run frequency and the warehouse — and watch the latency, the dollars, the failure odds and the on-call
            incidents update live.
          </p>
          <ul className="mt-6 space-y-3">
            {[
              "Per-issue cost, latency and incident modeling",
              "Tuned to Snowflake, BigQuery, Redshift & Databricks",
              "Quantifies the ROI of every fix in dollars and pages",
            ].map((t) => (
              <li key={t} className="flex items-start gap-3 text-ink">
                <span className="mt-1 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 border-ink bg-sage/20 text-sage">
                  <Check className="h-3 w-3" />
                </span>
                {t}
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded-2xl border-2 border-ink bg-paper2 p-6 shadow-block">
          <div className="flex items-center gap-2">
            <Zap className="h-4 w-4 text-terra" />
            <span className="font-display text-lg text-ink">Cartesian join</span>
            <span className="ml-auto rounded-full border border-line bg-paper3 px-2 py-0.5 font-mono text-[10px] text-inksoft">500M rows · 2×/day · Snowflake</span>
          </div>
          <div className="mt-5 space-y-4">
            {IMPACT_BARS.map((b) => (
              <div key={b.label}>
                <div className="flex items-baseline justify-between text-sm">
                  <span className="text-inksoft">{b.label}</span>
                  <span className="font-mono font-semibold text-ink">{b.value}</span>
                </div>
                <div className="mt-1.5 h-3 overflow-hidden rounded-full border border-line bg-paper3">
                  <div className={`h-full rounded-full ${b.tone}`} style={{ width: b.width }} />
                </div>
              </div>
            ))}
          </div>
          <blockquote className="mt-6 border-l-4 border-terra pl-4 font-display text-lg italic leading-snug text-ink">
            “On 500M rows at 2×/day, this cartesian join may increase Snowflake cost by ~$4,200/mo and trigger ~21
            incidents per month.”
          </blockquote>
        </div>
      </div>
    </section>
  );
}

/* ── How it works ────────────────────────────────────────────────────────── */

const STEPS = [
  { n: "01", title: "Paste your pipeline", body: "SQL, a dbt model, an Airflow DAG, a Spark job — we auto-detect the format from the code itself." },
  { n: "02", title: "We run the math", body: "85+ deterministic rules, warehouse cost modeling, column lineage and blast-radius simulation — in seconds, locally." },
  { n: "03", title: "Ship the fix", body: "Copy the diff, apply it, watch the score climb. Ask the AI to explain any finding in plain English." },
];

function How() {
  return (
    <section id="how" className="border-y-2 border-ink bg-paper2">
      <div className="mx-auto max-w-6xl px-5 py-20 lg:py-24">
        <div className="max-w-2xl">
          <Eyebrow>How it works</Eyebrow>
          <h2 className="mt-4 font-display text-3xl tracking-tight text-ink sm:text-4xl">
            From paste to fix in three steps.
          </h2>
        </div>
        <div className="mt-12 grid gap-6 md:grid-cols-3">
          {STEPS.map((s) => (
            <div key={s.n} className="rounded-2xl border-2 border-ink bg-paper p-6 shadow-block-sm">
              <span className="font-display text-5xl text-terra">{s.n}</span>
              <h3 className="mt-3 font-display text-xl text-ink">{s.title}</h3>
              <p className="mt-2 leading-relaxed text-inksoft">{s.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ── Why different ───────────────────────────────────────────────────────── */

const DIFFERENT = [
  { not: "Not a runtime monitor", body: "Monte Carlo, Bigeye and friends watch your data after it breaks. Copilot reads the code before it ever ships — no data required." },
  { not: "Not dbt-only", body: "One tool understands SQL, Airflow, dbt, Spark, Flink and Kafka. Most linters speak a single dialect." },
  { not: "Not a guessing chatbot", body: "An 85-rule engine is the source of truth. The AI only explains findings — it never invents tables or hallucinates problems." },
  { not: "Not a cloud SaaS", body: "Runs on your machine. No connector, no data egress, no account. Your code never leaves your laptop." },
];

function Different() {
  return (
    <section id="different" className="mx-auto max-w-6xl px-5 py-20 lg:py-28">
      <div className="max-w-2xl">
        <Eyebrow>Why it&apos;s different</Eyebrow>
        <h2 className="mt-4 font-display text-3xl tracking-tight text-ink sm:text-4xl">
          A different shape than everything else.
        </h2>
        <p className="mt-4 text-lg leading-relaxed text-inksoft">
          Most data tooling is a connected SaaS that watches data at runtime. Copilot is a static, code-first,
          local analyzer that catches the expensive mistakes before deploy.
        </p>
      </div>
      <div className="mt-12 grid gap-5 sm:grid-cols-2">
        {DIFFERENT.map((d) => (
          <div key={d.not} className="rounded-2xl border-2 border-ink bg-paper2 p-6 shadow-block-sm">
            <h3 className="font-display text-xl text-terra">{d.not}</h3>
            <p className="mt-2 leading-relaxed text-inksoft">{d.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Privacy band ────────────────────────────────────────────────────────── */

function Privacy() {
  return (
    <section className="border-y-2 border-ink bg-frost/10">
      <div className="mx-auto flex max-w-6xl flex-col items-center gap-4 px-5 py-16 text-center">
        <span className="inline-flex h-14 w-14 items-center justify-center rounded-2xl border-2 border-ink bg-paper2 text-frost shadow-block-sm">
          <Lock className="h-6 w-6" />
        </span>
        <h2 className="max-w-2xl font-display text-3xl tracking-tight text-ink sm:text-4xl">
          Your code never leaves your machine.
        </h2>
        <p className="max-w-xl text-lg leading-relaxed text-inksoft">
          The parser, the 85-rule engine and all the cost and impact math run locally. AI explanations are optional —
          point at a local model or a free key, or skip them entirely. Either way, nothing is sent to a third party.
        </p>
      </div>
    </section>
  );
}

/* ── Stats band ──────────────────────────────────────────────────────────── */

const STATS = [
  { v: "85+", k: "deterministic rules" },
  { v: "8", k: "frameworks" },
  { v: "9", k: "analysis dimensions" },
  { v: "~5s", k: "to full insight" },
  { v: "$0", k: "no account, no card" },
];

function Stats() {
  return (
    <section className="border-b-2 border-ink bg-terra text-paper2">
      <div className="mx-auto grid max-w-6xl grid-cols-2 gap-y-8 px-5 py-14 sm:grid-cols-3 lg:grid-cols-5">
        {STATS.map((s) => (
          <div key={s.k} className="text-center">
            <p className="font-display text-4xl sm:text-5xl">{s.v}</p>
            <p className="mt-1 text-xs uppercase tracking-widest text-paper2/80">{s.k}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Final CTA ───────────────────────────────────────────────────────────── */

function FinalCta() {
  return (
    <section className="mx-auto max-w-6xl px-5 py-24 text-center">
      <Squiggle className="mx-auto mb-10 max-w-xs text-terra" />
      <h2 className="mx-auto max-w-3xl font-display text-4xl leading-tight tracking-tight text-ink sm:text-5xl">
        Analyze your first pipeline.
      </h2>
      <p className="mx-auto mt-5 max-w-xl text-lg leading-relaxed text-inksoft">
        Paste a query, hit analyze, and in five seconds know exactly what it costs, what&apos;s broken, and how to fix it.
      </p>
      <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
        <CtaLink href={APP_HREF}>
          Analyze Pipeline
          <ArrowRight className="h-4 w-4" />
        </CtaLink>
        <CtaLink href={GITHUB_HREF} variant="outline" external>
          <GithubMark className="h-4 w-4" />
          Star on GitHub
        </CtaLink>
      </div>
    </section>
  );
}

/* ── Footer ──────────────────────────────────────────────────────────────── */

function Footer() {
  return (
    <footer className="border-t-2 border-ink bg-paper2">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-6 px-5 py-10 sm:flex-row">
        <div className="flex items-center gap-3">
          <Logo />
          <div className="leading-tight">
            <p className="font-display text-lg text-ink">Pipeline Copilot</p>
            <p className="text-xs text-inksoft">Deterministic data-pipeline intelligence.</p>
          </div>
        </div>
        <nav className="flex flex-wrap items-center gap-6 text-sm font-medium text-inksoft">
          <Link href={APP_HREF} className="transition-colors hover:text-ink">Open the analyzer</Link>
          <a href="#features" className="transition-colors hover:text-ink">Features</a>
          <a href="#different" className="transition-colors hover:text-ink">Why different</a>
          <a href={GITHUB_HREF} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 transition-colors hover:text-ink">
            <GithubMark className="h-4 w-4" /> GitHub
          </a>
        </nav>
      </div>
    </footer>
  );
}

/* ── Page ────────────────────────────────────────────────────────────────── */

export default function LandingPage() {
  return (
    <div className="bg-paper text-ink">
      <Nav />
      <main>
        <Hero />
        <Formats />
        <Features />
        <Determinism />
        <ImpactSpotlight />
        <How />
        <Different />
        <Privacy />
        <Stats />
        <FinalCta />
      </main>
      <Footer />
    </div>
  );
}
