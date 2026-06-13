"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

export interface CodeBlockProps {
  code: string;
  language?: string;
  title?: string;
  className?: string;
}

/** Heuristic: a unified diff has -/+/@@ markers or ---/+++ file headers. */
function looksLikeDiff(code: string): boolean {
  return /^(---|\+\+\+|@@|[+-])/m.test(code) && /^[+-]/m.test(code);
}

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "font-bold text-inksoft";
  }
  if (line.startsWith("@@")) return "text-frost";
  if (line.startsWith("+")) return "bg-sage/10 text-ink";
  if (line.startsWith("-")) return "bg-terra/10 text-ink";
  return "text-ink";
}

export function CodeBlock({
  code,
  language,
  title,
  className,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const isDiff = looksLikeDiff(code);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard may be unavailable; ignore */
    }
  }

  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border border-line bg-paper3",
        className,
      )}
    >
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="truncate font-mono text-xs font-medium uppercase tracking-wide text-inksoft">
          {title ?? language ?? "code"}
        </span>
        <button
          type="button"
          onClick={copy}
          aria-label={copied ? "Copied" : "Copy code"}
          className={cn(
            "inline-flex items-center gap-1 rounded-lg border-2 border-ink bg-paper2 px-2 py-1",
            "text-xs font-medium text-ink transition hover:bg-paper3",
            "active:translate-x-[1px] active:translate-y-[1px]",
          )}
        >
          {copied ? (
            <>
              <Check aria-hidden="true" className="h-3.5 w-3.5 text-sage" />
              Copied
            </>
          ) : (
            <>
              <Copy aria-hidden="true" className="h-3.5 w-3.5" />
              Copy
            </>
          )}
        </button>
      </div>
      <pre className="overflow-x-auto px-3 py-3 font-mono text-sm leading-relaxed">
        {isDiff ? (
          <code>
            {code.split("\n").map((line, i) => (
              <span
                key={i}
                className={cn("-mx-3 block px-3", lineClass(line))}
              >
                {line.length ? line : " "}
              </span>
            ))}
          </code>
        ) : (
          <code className="text-ink">{code}</code>
        )}
      </pre>
    </div>
  );
}
