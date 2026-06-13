"use client";

import { useId } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  label?: string;
  id?: string;
  className?: string;
  disabled?: boolean;
}

export function Select({
  value,
  onChange,
  options,
  label,
  id,
  className,
  disabled,
}: SelectProps) {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {label && (
        <label
          htmlFor={selectId}
          className="text-xs font-medium uppercase tracking-wide text-inksoft"
        >
          {label}
        </label>
      )}
      <div className="relative">
        <select
          id={selectId}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className={cn(
            "w-full appearance-none rounded-xl border-2 border-ink bg-paper2 py-2 pl-3 pr-9",
            "text-sm font-medium text-ink shadow-block-sm transition",
            "hover:bg-paper3/60 focus:outline-none",
            "disabled:cursor-not-allowed disabled:opacity-55",
          )}
        >
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <ChevronDown
          aria-hidden="true"
          className="pointer-events-none absolute right-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-inksoft"
        />
      </div>
    </div>
  );
}
