"use client";

import { useId } from "react";
import { cn } from "@/lib/utils";

export interface SliderProps {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  label?: string;
  format?: (value: number) => string;
  id?: string;
  className?: string;
  disabled?: boolean;
}

export function Slider({
  value,
  onChange,
  min,
  max,
  step = 1,
  label,
  format,
  id,
  className,
  disabled,
}: SliderProps) {
  const generatedId = useId();
  const sliderId = id ?? generatedId;
  const display = format ? format(value) : String(value);
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {(label || format) && (
        <div className="flex items-baseline justify-between gap-2">
          {label && (
            <label
              htmlFor={sliderId}
              className="text-xs font-medium uppercase tracking-wide text-inksoft"
            >
              {label}
            </label>
          )}
          <span className="font-mono text-sm font-semibold tabular-nums text-ink">
            {display}
          </span>
        </div>
      )}
      <input
        id={sliderId}
        type="range"
        className="copilot-range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        aria-valuetext={display}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
