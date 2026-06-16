"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * Pure CSS/React tooltip - no portal, no library. The bubble fades in on
 * group-hover/focus after a short delay and sits centered above the trigger.
 */
export function Tooltip({ content, children, className }: TooltipProps) {
  return (
    <span className={cn("group relative inline-flex", className)}>
      {children}
      <span
        role="tooltip"
        className={cn(
          "pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 -translate-x-1/2",
          "max-w-xs whitespace-normal rounded-xl border-2 border-ink bg-paper2 px-3 py-2",
          "text-xs leading-snug text-ink shadow-block-sm",
          "opacity-0 translate-y-1 transition-all duration-150 delay-300",
          "group-hover:opacity-100 group-hover:translate-y-0 group-hover:delay-500",
          "group-focus-within:opacity-100 group-focus-within:translate-y-0",
        )}
      >
        {content}
        <span
          aria-hidden="true"
          className="absolute left-1/2 top-full -mt-1 h-2 w-2 -translate-x-1/2 rotate-45 border-b-2 border-r-2 border-ink bg-paper2"
        />
      </span>
    </span>
  );
}
