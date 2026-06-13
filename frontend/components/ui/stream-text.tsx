"use client";

import { cn } from "@/lib/utils";

export interface StreamTextProps {
  text: string;
  streaming: boolean;
  className?: string;
}

/**
 * Renders streamed LLM text with preserved whitespace and a blinking caret that
 * trails the output while a stream is in flight.
 */
export function StreamText({
  text,
  streaming,
  className,
}: StreamTextProps) {
  return (
    <div
      aria-live="polite"
      className={cn(
        "whitespace-pre-wrap break-words text-sm leading-relaxed text-ink",
        className,
      )}
    >
      {text}
      {streaming && (
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block h-[1.05em] w-[2px] translate-y-[2px] bg-terra align-text-bottom [animation:var(--animate-caret-blink)]"
        />
      )}
    </div>
  );
}
