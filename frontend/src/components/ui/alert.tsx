import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export type AlertTone = "info" | "warning" | "danger";

const tones: Record<AlertTone, string> = {
  info: "border-slate-300 bg-slate-50 text-slate-900",
  warning: "border-amber-400 bg-amber-50 text-amber-950",
  danger: "border-red-400 bg-red-50 text-red-950",
};

/** The caller sets `role` ("status" for polite updates, "alert" for failures). */
export function Alert({ tone = "info", className, ...props }: ComponentProps<"div"> & { tone?: AlertTone }) {
  return <div className={cn("rounded-lg border-l-4 border p-4", tones[tone], className)} {...props} />;
}

export function AlertTitle({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("mb-1 font-semibold", className)} {...props} />;
}
