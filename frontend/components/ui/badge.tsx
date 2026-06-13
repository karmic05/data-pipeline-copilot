import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export type BadgeTone = "terra" | "ochre" | "frost" | "sage" | "ink";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

const TONES: Record<BadgeTone, string> = {
  terra: "bg-terra/10 text-terra border border-terra/40",
  ochre: "bg-ochre/15 text-ochre border border-ochre/45",
  frost: "bg-frost/10 text-frost border border-frost/40",
  sage: "bg-sage/15 text-sage border border-sage/45",
  ink: "bg-ink/8 text-ink border border-ink/30",
};

export function Badge({
  tone = "ink",
  className,
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium leading-5 tracking-wide",
        TONES[tone],
        className,
      )}
      {...props}
    />
  );
}
