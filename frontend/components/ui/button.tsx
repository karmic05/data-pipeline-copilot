"use client";

import { forwardRef, type ButtonHTMLAttributes } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "outline" | "ghost";
  size?: "sm" | "md";
  loading?: boolean;
}

const VARIANTS: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary:
    "border-2 border-ink bg-terra text-paper2 shadow-block-sm hover:brightness-105 active:translate-x-[2px] active:translate-y-[2px] active:shadow-none",
  outline:
    "border-2 border-ink bg-paper2 text-ink shadow-block-sm hover:bg-paper3 active:translate-x-[2px] active:translate-y-[2px] active:shadow-none",
  ghost:
    "border-2 border-transparent bg-transparent text-ink hover:bg-paper3/70",
};

const SIZES: Record<NonNullable<ButtonProps["size"]>, string> = {
  sm: "h-8 gap-1.5 px-3 text-xs",
  md: "h-10 gap-2 px-4 text-sm",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "primary",
      size = "md",
      loading = false,
      disabled,
      className,
      children,
      type = "button",
      ...props
    },
    ref,
  ) {
    const isDisabled = disabled || loading;
    return (
      <button
        ref={ref}
        type={type}
        disabled={isDisabled}
        aria-busy={loading || undefined}
        className={cn(
          "inline-flex select-none items-center justify-center rounded-xl font-medium transition",
          "disabled:cursor-not-allowed disabled:opacity-55 disabled:active:translate-x-0 disabled:active:translate-y-0",
          SIZES[size],
          VARIANTS[variant],
          className,
        )}
        {...props}
      >
        {loading && (
          <Loader2
            aria-hidden="true"
            className={size === "sm" ? "h-3.5 w-3.5 animate-spin" : "h-4 w-4 animate-spin"}
          />
        )}
        {children}
      </button>
    );
  },
);
