import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

/** A semantic table inside its own labeled, keyboard-scrollable region. */
export function Table({ className, label, ...props }: ComponentProps<"table"> & { label: string }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className="max-w-full overflow-x-auto rounded-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700">
      <table className={cn("w-full border-collapse text-left text-sm", className)} {...props} />
    </div>
  );
}

export function TableCaption({ className, ...props }: ComponentProps<"caption">) {
  return <caption className={cn("pb-2 text-left text-sm text-slate-600", className)} {...props} />;
}

export function TableHead({ className, ...props }: ComponentProps<"th">) {
  return <th scope="col" className={cn("border-b border-slate-300 px-3 py-2 font-semibold text-slate-800", className)} {...props} />;
}

export function TableCell({ className, ...props }: ComponentProps<"td">) {
  return <td className={cn("border-b border-slate-200 px-3 py-2 text-slate-900", className)} {...props} />;
}
