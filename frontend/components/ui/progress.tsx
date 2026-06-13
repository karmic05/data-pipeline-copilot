import { cn } from "@/lib/utils";

export type MeterTone = "terra" | "ochre" | "frost" | "sage" | "plum" | "ink";

export interface MeterProps {
  value: number;
  max: number;
  tone?: MeterTone;
  label?: string;
  className?: string;
}

const FILLS: Record<MeterTone, string> = {
  terra: "bg-terra",
  ochre: "bg-ochre",
  frost: "bg-frost",
  sage: "bg-sage",
  plum: "bg-plum",
  ink: "bg-ink",
};

export function Meter({
  value,
  max,
  tone = "frost",
  label,
  className,
}: MeterProps) {
  const safeMax = max > 0 ? max : 1;
  const pct = Math.max(0, Math.min(100, (value / safeMax) * 100));
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {label && (
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-inksoft">
            {label}
          </span>
          <span className="font-mono text-xs font-semibold tabular-nums text-ink">
            {Math.round(value)}
            <span className="text-inksoft">/{max}</span>
          </span>
        </div>
      )}
      <div
        role="meter"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-label={label}
        className="h-3 w-full overflow-hidden rounded-full border-2 border-ink/70 bg-paper3"
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-700 ease-out",
            FILLS[tone],
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
