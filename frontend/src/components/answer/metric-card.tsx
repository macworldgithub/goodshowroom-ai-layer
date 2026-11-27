import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { formatCount } from "./format";

/** One verified number. A verified zero shows `0`, never a blank or a dash. */
export function MetricCard({ label, value, unit, period }: { label: string; value: number; unit: string; period: string }) {
  return (
    <Card role="group" aria-label={`${label}: ${formatCount(value)} ${unit}`}>
      <CardHeader>
        <CardTitle>{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-4xl font-semibold tracking-tight">{formatCount(value)}</p>
        <p className="mt-1 text-sm text-slate-700">{unit}</p>
        <p className="mt-3 text-sm text-slate-600">{period}</p>
      </CardContent>
    </Card>
  );
}
