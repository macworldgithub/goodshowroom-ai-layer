import { Table, TableCaption, TableCell, TableHead } from "../ui/table";
import { formatCount } from "./format";

export type TableColumn = { key: string; label: string; type: "string" | "number"; unit?: string | null };
export type TableRow = Record<string, string | number | null | undefined>;

/** Rows beyond this bound are not drawn. The API bound is smaller; this is a second guard. */
export const MAX_TABLE_ROWS = 200;

function cell(column: TableColumn, value: TableRow[string]): string {
  if (column.type === "number") return typeof value === "number" && Number.isFinite(value) ? formatCount(value) : "No value";
  return typeof value === "string" ? value : "No value";
}

/** A read-only semantic table. Values are shown exactly as the backend sent them. */
export function DatasetTable({ caption, columns, rows }: { caption: string; columns: readonly TableColumn[]; rows: readonly TableRow[] }) {
  const shown = rows.slice(0, MAX_TABLE_ROWS);
  return (
    <div>
      <Table label={caption}>
        <TableCaption>{caption}</TableCaption>
        <thead>
          <tr>
            {columns.map((column) => (
              <TableHead key={column.key}>{column.unit ? `${column.label} (${column.unit})` : column.label}</TableHead>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <TableCell key={column.key} className={column.type === "number" ? "tabular-nums" : undefined}>{cell(column, row[column.key])}</TableCell>
              ))}
            </tr>
          ))}
        </tbody>
      </Table>
      {rows.length > shown.length ? <p className="mt-2 text-sm text-slate-700">Showing the first {formatCount(shown.length)} rows.</p> : null}
    </div>
  );
}
