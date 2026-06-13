import type { HTMLAttributes } from "react";
import type { Severity } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface SeverityBadgeProps
  extends HTMLAttributes<HTMLSpanElement> {
  severity: Severity;
}

const STYLES: Record<Severity, { wrap: string; dot: string }> = {
  CRITICAL: {
    wrap: "bg-terra text-paper2 border border-ink/80",
    dot: "bg-paper2",
  },
  WARNING: {
    wrap: "bg-ochre/15 text-ochre border border-ochre/50",
    dot: "bg-ochre",
  },
  INFO: {
    wrap: "bg-frost/12 text-frost border border-frost/45",
    dot: "bg-frost",
  },
};

export function SeverityBadge({
  severity,
  className,
  ...props
}: SeverityBadgeProps) {
  const s = STYLES[severity];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide",
        s.wrap,
        className,
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn("h-1.5 w-1.5 rounded-full", s.dot)}
      />
      {severity}
    </span>
  );
}
