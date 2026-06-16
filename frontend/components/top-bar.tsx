"use client";

/**
 * Top application bar: wordmark, detected-format chip, provider status, and the
 * Analyze button. Reads everything from the shared analysis store.
 */
import { useState } from "react";
import Link from "next/link";
import { Database, Play, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/tooltip";
import { useAnalysis } from "@/lib/store";

function Wordmark() {
  return (
    <Link
      href="/"
      className="flex items-center gap-3 rounded-lg transition-opacity hover:opacity-80"
      aria-label="Pipeline Copilot home"
    >
      <span
        className="relative inline-block h-7 w-7 shrink-0"
        aria-hidden="true"
      >
        <span className="absolute left-0 top-1 h-4 w-4 rotate-12 rounded-[4px] border-2 border-ink bg-terra" />
        <span className="absolute left-2 top-0 h-4 w-4 -rotate-6 rounded-[4px] border-2 border-ink bg-frost" />
        <span className="absolute left-1 top-3 h-4 w-4 rotate-[20deg] rounded-[4px] border-2 border-ink bg-ochre" />
      </span>
      <span className="flex flex-col leading-none">
        <span className="text-[10px] uppercase tracking-widest text-inksoft">
          data intelligence
        </span>
        <span className="font-display text-xl text-ink">Pipeline Copilot</span>
      </span>
    </Link>
  );
}

export function TopBar() {
  const { report, provider, analyzing, error, runAnalyze, connection } =
    useAnalysis();
  const [dismissed, setDismissed] = useState(false);

  // Surface every fresh error: reset the local dismissal whenever it changes.
  // We adjust state during render (tracking the previous error in state, the
  // React-recommended pattern) instead of in an effect, so a new error is shown
  // immediately without an extra render pass.
  const [seenError, setSeenError] = useState<string | null>(error);
  if (seenError !== error) {
    setSeenError(error);
    setDismissed(false);
  }

  const dialectSuffix = report?.dialect ? ` · ${report.dialect}` : "";
  const allWarnings = report?.parser_warnings ?? [];
  // Live-grounding notes (prefixed "[live]") are informational, not parser
  // problems - keep them out of the warning count.
  const warnings = allWarnings.filter((w) => !w.startsWith("[live]"));
  const showError = error !== null && !dismissed;

  return (
    <header className="shrink-0">
      <div className="flex items-center gap-4 border-b-2 border-ink bg-paper2 px-5 py-3">
        <Wordmark />

        {connection ? (
          <Tooltip content="Analyses are grounded in this live database (real schemas + profiled cost). Manage it in the Connect tab.">
            <span className="chip inline-flex items-center gap-1.5 rounded-full border border-frost/50 bg-frost/10 px-3 font-mono text-xs text-frost">
              <Database aria-hidden="true" className="h-3 w-3" />
              live · {connection.kind}
            </span>
          </Tooltip>
        ) : null}

        {report ? (
          <span className="flex items-center gap-2">
            <span className="chip rounded-full border border-line px-3 font-mono text-xs text-ink">
              {report.format}
              {dialectSuffix}
            </span>
            {warnings.length > 0 ? (
              <Tooltip
                content={
                  <ul className="space-y-1">
                    {warnings.map((w, i) => (
                      <li key={i} className="text-xs">
                        {w}
                      </li>
                    ))}
                  </ul>
                }
              >
                <span className="chip rounded-full border border-ochre/50 bg-ochre/15 px-3 text-xs font-medium text-ochre">
                  {warnings.length} parser{" "}
                  {warnings.length === 1 ? "note" : "notes"}
                </span>
              </Tooltip>
            ) : null}
          </span>
        ) : null}

        <div className="ml-auto flex items-center gap-4">
          {provider ? (
            <Tooltip content={provider.detail}>
              <span className="flex items-center gap-2">
                <span
                  className={`inline-block h-2.5 w-2.5 rounded-full animate-pulse ${
                    provider.available ? "bg-sage" : "bg-terra"
                  }`}
                  aria-hidden="true"
                />
                <span className="font-mono text-xs text-inksoft">
                  {provider.provider} / {provider.model}
                </span>
              </span>
            </Tooltip>
          ) : null}

          <Button
            variant="primary"
            size="md"
            loading={analyzing}
            onClick={runAnalyze}
          >
            <Play className="h-4 w-4" aria-hidden="true" />
            {analyzing ? "Analyzing…" : "Analyze"}
          </Button>
        </div>
      </div>

      {showError ? (
        <div className="flex items-center gap-3 border-b border-terra/30 bg-terra/10 px-5 py-2 text-terra">
          <span className="flex-1 text-sm">{error}</span>
          <button
            type="button"
            onClick={() => setDismissed(true)}
            aria-label="Dismiss error"
            className="rounded-full p-1 transition-colors hover:bg-terra/15"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      ) : null}
    </header>
  );
}

export default TopBar;
