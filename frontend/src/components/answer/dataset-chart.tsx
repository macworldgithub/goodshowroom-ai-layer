"use client";

import { Bar, BarChart, CartesianGrid, Line, LineChart, LabelList, Tooltip, XAxis, YAxis } from "recharts";
import { DatasetTable, type TableColumn, type TableRow } from "./dataset-table";
import { formatCount } from "./format";

export type ChartKind = "bar" | "line";
export type ChartProps = {
  kind: ChartKind;
  title: string;
  /** The category (bar) or date (line) column key, and the numeric value column key. */
  categoryKey: string;
  valueKey: string;
  columns: readonly TableColumn[];
  rows: readonly TableRow[];
  /** Fixed size for tests and print. Without it the chart fills its container. */
  width?: number;
  height?: number;
};

/** A line needs several verified observations. Fewer than this show the table only. */
export const MIN_LINE_POINTS = 3;
export const MAX_CHART_ROWS = 100;
// One fixed color from code. Meaning never depends on color: values are labeled and tabled.
const SERIES_COLOR = "#1d4ed8";

type Point = { category: string; value: number | null };

function points(props: ChartProps): Point[] | null {
  if (props.rows.length === 0 || props.rows.length > MAX_CHART_ROWS) return null;
  const column = props.columns.find((item) => item.key === props.valueKey);
  if (!column || column.type !== "number" || !props.columns.some((item) => item.key === props.categoryKey)) return null;
  const result: Point[] = [];
  for (const row of props.rows) {
    const category = row[props.categoryKey];
    const value = row[props.valueKey];
    if (typeof category !== "string" || !(value === null || value === undefined || (typeof value === "number" && Number.isFinite(value)))) return null;
    // A missing value stays a gap. It is never turned into zero.
    result.push({ category, value: value ?? null });
  }
  return result;
}

/**
 * A bar or line chart from one validated dataset. Chart code and styles are
 * fixed here. Data only supplies text and numbers, which React and SVG escape.
 * The picture is hidden from screen readers; the table below carries every value.
 */
export function DatasetChart(props: ChartProps) {
  const data = points(props);
  const unit = props.columns.find((item) => item.key === props.valueKey)?.unit ?? "";
  if (data === null) return <DatasetTable caption={props.title} columns={props.columns} rows={props.rows} />;
  const observed = data.filter((point) => point.value !== null).length;
  const lineTooShort = props.kind === "line" && observed < MIN_LINE_POINTS;
  const common = { data, margin: { top: 20, right: 12, bottom: 8, left: 4 }, ...(props.width && props.height ? { width: props.width, height: props.height } : { responsive: true, style: { width: "100%", height: props.height ?? 260 } }) };
  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" vertical={false} />
      <XAxis dataKey="category" tickLine={false} />
      <YAxis allowDecimals={false} tickFormatter={(value: number) => formatCount(value)} width={56} />
      <Tooltip formatter={(value) => (typeof value === "number" ? `${formatCount(value)}${unit ? ` ${unit}` : ""}` : "No value")} />
    </>
  );
  return (
    <figure aria-label={props.title} className="space-y-3">
      <figcaption className="text-sm text-slate-700">
        {props.title}. {formatCount(data.length)} {data.length === 1 ? "entry" : "entries"}. The table below lists every value.
      </figcaption>
      {lineTooShort ? (
        <p role="note" className="text-sm text-slate-700">There are not enough verified observations to draw a line chart.</p>
      ) : (
        <div aria-hidden="true" data-chart={props.kind}>
          {props.kind === "bar" ? (
            <BarChart {...common}>
              {axes}
              <Bar dataKey="value" name={props.columns.find((item) => item.key === props.valueKey)?.label} fill={SERIES_COLOR} isAnimationActive={false}>
                <LabelList dataKey="value" position="top" formatter={(value) => (typeof value === "number" ? formatCount(value) : "")} />
              </Bar>
            </BarChart>
          ) : (
            <LineChart {...common}>
              {axes}
              <Line dataKey="value" name={props.columns.find((item) => item.key === props.valueKey)?.label} stroke={SERIES_COLOR} connectNulls={false} dot isAnimationActive={false}>
                <LabelList dataKey="value" position="top" formatter={(value) => (typeof value === "number" ? formatCount(value) : "")} />
              </Line>
            </LineChart>
          )}
        </div>
      )}
      <DatasetTable caption={`${props.title}: data table`} columns={props.columns} rows={props.rows} />
    </figure>
  );
}
