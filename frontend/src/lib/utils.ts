import { clsx, type ClassValue } from "clsx";

/** Join class names. Class names are fixed strings in code, never built from API data. */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
