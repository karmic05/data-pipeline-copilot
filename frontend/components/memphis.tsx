/**
 * Decorative Memphis-style SVG accents. All purely ornamental (aria-hidden) and
 * inherit `currentColor`, so callers tint them with text-terra / text-frost etc.
 */
import type { SVGProps } from "react";
import { cn } from "@/lib/utils";

/** A horizontal wavy divider rule. */
export function Squiggle({
  className,
  ...props
}: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 240 12"
      fill="none"
      preserveAspectRatio="none"
      className={cn("h-3 w-full text-line", className)}
      {...props}
    >
      <path
        d="M0 6C10 0 20 0 30 6C40 12 50 12 60 6C70 0 80 0 90 6C100 12 110 12 120 6C130 0 140 0 150 6C160 12 170 12 180 6C190 0 200 0 210 6C220 12 230 12 240 6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** A small scattered dot grid block. */
export function DotGrid({
  className,
  rows = 4,
  cols = 6,
  ...props
}: SVGProps<SVGSVGElement> & { rows?: number; cols?: number }) {
  const gap = 12;
  const r = 2;
  const dots = [];
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      dots.push(
        <circle
          key={`${x}-${y}`}
          cx={x * gap + r + 1}
          cy={y * gap + r + 1}
          r={r}
          fill="currentColor"
        />,
      );
    }
  }
  return (
    <svg
      aria-hidden="true"
      viewBox={`0 0 ${cols * gap} ${rows * gap}`}
      width={cols * gap}
      height={rows * gap}
      className={cn("text-line", className)}
      {...props}
    >
      {dots}
    </svg>
  );
}

/**
 * A scattered burst of triangle / circle / cross shapes for empty states.
 * Each shape uses a distinct token color so it reads as playful confetti.
 */
export function ShapeBurst({
  className,
  ...props
}: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 160 120"
      fill="none"
      className={cn("h-28 w-40", className)}
      {...props}
    >
      {/* circle */}
      <circle
        cx="34"
        cy="34"
        r="16"
        className="fill-frost/15 stroke-frost"
        strokeWidth="2.5"
      />
      {/* triangle */}
      <path
        d="M118 16L138 52H98L118 16Z"
        className="fill-ochre/20 stroke-ochre"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
      {/* cross */}
      <path
        d="M124 86V108M113 97H135"
        className="stroke-terra"
        strokeWidth="3"
        strokeLinecap="round"
      />
      {/* small square */}
      <rect
        x="22"
        y="80"
        width="24"
        height="24"
        rx="4"
        className="fill-sage/20 stroke-sage"
        strokeWidth="2.5"
      />
      {/* plum dot */}
      <circle cx="78" cy="62" r="6" className="fill-plum" />
      {/* squiggle */}
      <path
        d="M58 100C64 94 70 106 76 100C82 94 88 106 94 100"
        className="stroke-plum"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * No-op fragment kept for API completeness - the paper grain lives in globals.css
 * via the body-level `.paper-texture` overlay, so nothing needs to render here.
 */
export function PaperTexture(): null {
  return null;
}
