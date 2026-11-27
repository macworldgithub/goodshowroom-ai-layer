import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

// Owned copy of the shadcn/ui Card. Edit here; no runtime dependency.
export function Card({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("rounded-xl border border-slate-200 bg-white text-slate-900", className)} {...props} />;
}

export function CardHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex flex-col gap-1 p-5 pb-2", className)} {...props} />;
}

export function CardTitle({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("text-sm font-medium text-slate-600", className)} {...props} />;
}

export function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("p-5 pt-0", className)} {...props} />;
}
