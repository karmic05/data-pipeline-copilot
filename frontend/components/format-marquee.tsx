/**
 * Infinite, theme-tinted logo marquee for the landing page.
 *
 * Real brand marks come from `simple-icons` (single-path silhouettes), so they
 * tint uniformly to the page ink - no neon brand colors. Brands that simple-
 * icons no longer ships (Redshift, dbt, Dagster, Great Expectations) and the
 * generic "SQL" fall back to matching lucide line icons, so the row stays
 * visually consistent. The seamless scroll is pure CSS (see .marquee in
 * globals.css): two identical tracks translating -100%, paused on hover, and
 * disabled under prefers-reduced-motion.
 */
import type { LucideIcon } from "lucide-react";
import { Asterisk, Boxes, Database, FlaskConical, Warehouse } from "lucide-react";
import {
  siApacheairflow,
  siApacheflink,
  siApachekafka,
  siApachespark,
  siGooglebigquery,
  siPrefect,
  siSnowflake,
} from "simple-icons";

type Item = { label: string; path?: string; Icon?: LucideIcon };

const ITEMS: Item[] = [
  { label: "SQL", Icon: Database },
  { label: "Snowflake", path: siSnowflake.path },
  { label: "BigQuery", path: siGooglebigquery.path },
  { label: "Redshift", Icon: Warehouse },
  { label: "Airflow", path: siApacheairflow.path },
  { label: "dbt", Icon: Boxes },
  { label: "Spark", path: siApachespark.path },
  { label: "Flink", path: siApacheflink.path },
  { label: "Kafka", path: siApachekafka.path },
  { label: "Prefect", path: siPrefect.path },
  { label: "Dagster", Icon: Asterisk },
  { label: "Great Expectations", Icon: FlaskConical },
];

function BrandIcon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" className="h-6 w-6 shrink-0">
      <path d={path} />
    </svg>
  );
}

function MarqueeRow({ ariaHidden = false }: { ariaHidden?: boolean }) {
  return (
    <ul
      className="marquee-track flex w-max shrink-0 items-center"
      aria-hidden={ariaHidden || undefined}
    >
      {ITEMS.map((item, i) => (
        <li
          key={`${item.label}-${i}`}
          className="flex items-center gap-2.5 px-7 text-ink/80 transition-colors hover:text-terra"
        >
          {item.path ? (
            <BrandIcon path={item.path} />
          ) : item.Icon ? (
            <item.Icon className="h-6 w-6 shrink-0" strokeWidth={1.75} aria-hidden="true" />
          ) : null}
          <span className="whitespace-nowrap font-display text-lg">{item.label}</span>
        </li>
      ))}
    </ul>
  );
}

export function FormatMarquee() {
  return (
    <section className="border-y-2 border-ink bg-paper2 py-10">
      <p className="px-5 text-center text-xs font-semibold uppercase tracking-widest text-inksoft">
        Understands your whole stack - auto-detected from the code
      </p>
      <div className="marquee group relative mt-8 flex overflow-hidden">
        {/* edge fades for the premium logo-cloud look */}
        <div className="pointer-events-none absolute inset-y-0 left-0 z-10 w-20 bg-gradient-to-r from-paper2 to-transparent sm:w-32" />
        <div className="pointer-events-none absolute inset-y-0 right-0 z-10 w-20 bg-gradient-to-l from-paper2 to-transparent sm:w-32" />
        <MarqueeRow />
        <MarqueeRow ariaHidden />
      </div>
    </section>
  );
}

export default FormatMarquee;
