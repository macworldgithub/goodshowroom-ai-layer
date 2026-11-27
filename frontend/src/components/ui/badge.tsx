import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export type BadgeTone = "neutral" | "good" | "warning" | "danger";

const tones: Record<BadgeTone, string> = {
  neutral: "border-slate-300 bg-slate-50 text-slate-800",
  good: "border-emerald-300 bg-emerald-50 text-emerald-900",
  warning: "border-amber-300 bg-amber-50 text-amber-900",
  danger: "border-red-300 bg-red-50 text-red-900",
};

/** A status label. The meaning is always in the text, never in the color alone. */
export function Badge({ tone = "neutral", className, ...props }: ComponentProps<"span"> & { tone?: BadgeTone }) {
  return <span className={cn("inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold", tones[tone], className)} {...props} />;
}
