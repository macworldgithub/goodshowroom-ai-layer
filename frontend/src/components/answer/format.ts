/** Display-only formatting. No business value is calculated here. */

const count = new Intl.NumberFormat("en-US");

/** An exact whole number with thousands separators. Zero stays "0". */
export function formatCount(value: number): string {
  return count.format(value);
}

const day = new Intl.DateTimeFormat("en-AU", { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" });

/** Format a `YYYY-MM-DD` date that is already in the dealership's local calendar. */
function formatLocalDate(isoDate: string): string {
  return day.format(new Date(`${isoDate}T00:00:00Z`));
}

/**
 * Show the real calendar days of a period. The end instant is exclusive, so the
 * last shown day is the day before `end_local`.
 */
export function formatPeriodDays(startLocal: string, endLocal: string): string {
  const first = startLocal.slice(0, 10);
  const last = new Date(`${endLocal.slice(0, 10)}T00:00:00Z`);
  last.setUTCDate(last.getUTCDate() - 1);
  return `${formatLocalDate(first)} to ${formatLocalDate(last.toISOString().slice(0, 10))}`;
}

/** The retrieval time in the dealership's own timezone, with the zone name. */
export function formatRetrieved(instant: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-AU", { timeZone, day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(instant));
}
