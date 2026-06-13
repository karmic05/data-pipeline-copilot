/**
 * Shared className helper. Merges conditional clsx input with tailwind-merge so
 * later utility classes win over earlier conflicting ones.
 */
import clsx, { type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
