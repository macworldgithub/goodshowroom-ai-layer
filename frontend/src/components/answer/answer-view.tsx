import { responseValid, type components } from "../../lib/api-contracts";
import { Alert, AlertTitle } from "../ui/alert";
import { Badge, type BadgeTone } from "../ui/badge";
import { DatasetTable, type TableColumn, type TableRow } from "./dataset-table";
import { formatPeriodDays, formatRetrieved } from "./format";
import { MetricCard } from "./metric-card";
import { SafeMarkdown } from "./safe-markdown";

type Response = components["schemas"]["StructuredResponse"];
type Dataset = components["schemas"]["Dataset"];

const noticeTitles: Record<string, string> = {
  incomplete_data: "Partial result",
  missing_data: "Data unavailable",
  question_not_supported: "Not supported yet",
  scope_required: "One detail is needed",
  disclosure_withheld: "Result withheld",
};

const coverageText: Record<Response["coverage"]["status"], [string, BadgeTone]> = {
  complete: ["Coverage: complete", "good"],
  partial: ["Coverage: partial", "warning"],
  unknown: ["Coverage: unknown", "neutral"],
  suppressed: ["Coverage: withheld", "warning"],
};

function table(dataset: Dataset): { columns: TableColumn[]; rows: TableRow[] } {
  return {
    columns: dataset.columns.map((column) => ({ key: column.key, label: column.label, type: column.type, unit: column.unit })),
    rows: dataset.rows.map((row) => ({ ...row })),
  };
}

function Invalid() {
  return (
    <Alert tone="danger" role="alert" data-state="invalid">
      <AlertTitle>This answer could not be validated</AlertTitle>
      <p>No result is shown. Ask the question again.</p>
    </Alert>
  );
}

/**
 * Draw one checked answer. The payload is checked again here. If it does not
 * match the approved rules, nothing from it is shown. Only the approved visual
 * kinds (`card`, `table`) are drawn, each from the same backend facts.
 */
export function AnswerView({ response, snapshot = false }: { response: Response; snapshot?: boolean }) {
  try {
    responseValid(response);
  } catch {
    return <Invalid />;
  }
  const [coverageLabel, coverageTone] = coverageText[response.coverage.status];

  if (response.status !== "complete") {
    const warning = response.warnings[0] ?? "";
    const title = noticeTitles[warning] ?? "No result";
    return (
      <article data-state={warning || response.status} aria-label={title} className="space-y-3">
        <Alert tone={response.status === "partial" ? "warning" : "info"} role="status" data-state={response.status}>
          <AlertTitle>{title}</AlertTitle>
          <SafeMarkdown>{response.answer}</SafeMarkdown>
        </Alert>
        <Badge tone={coverageTone}>{coverageLabel}</Badge>
      </article>
    );
  }

  // The checks above guarantee these exist and agree with each other.
  const { period, scope, retrieved_at: retrieved } = response;
  if (!period || !scope || !retrieved) return <Invalid />;
  const dataset = response.datasets[0];
  const row = dataset.rows[0];
  const valueColumn = dataset.columns.find((column) => column.key === "value");
  const days = formatPeriodDays(period.start_local, period.end_local);
  const { columns, rows } = table(dataset);

  return (
    <article data-state="complete" aria-label="Answer" className="space-y-4">
      <SafeMarkdown>{response.answer}</SafeMarkdown>
      {response.visuals.map((visual) =>
        visual.kind === "card" ? (
          <MetricCard key="card" label={String(row[visual.field_bindings.label])} value={Number(row[visual.field_bindings.value])} unit={valueColumn?.unit ?? ""} period={days} />
        ) : (
          <DatasetTable key="table" caption={`${row.label}, ${days}`} columns={columns} rows={rows} />
        ),
      )}
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
        <dt className="font-semibold">Period</dt>
        <dd>{period.label}: {days}</dd>
        <dt className="font-semibold">Timezone</dt>
        <dd>{period.timezone}</dd>
        <dt className="font-semibold">Scope</dt>
        <dd>{scope.site} (full permitted site)</dd>
        <dt className="font-semibold">Source</dt>
        <dd>{response.sources[0].label}</dd>
        <dt className="font-semibold">Retrieved</dt>
        <dd><time dateTime={retrieved}>{formatRetrieved(retrieved, period.timezone)}</time></dd>
      </dl>
      <Badge tone={coverageTone}>{coverageLabel}</Badge>
      {snapshot ? <p className="text-sm text-slate-700">This is a dated snapshot. Ask again to get fresh data.</p> : null}
      <details className="text-sm">
        <summary className="cursor-pointer font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700">Evidence details</summary>
        <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-[max-content_1fr]">
          <dt className="font-semibold">Metric</dt>
          <dd>{response.evidence[0].metric_id} (version {response.evidence[0].metric_version})</dd>
          <dt className="font-semibold">Basis</dt>
          <dd>Reviewed metric definition</dd>
          <dt className="font-semibold">Claim</dt>
          <dd>{response.claims[0].text}</dd>
        </dl>
      </details>
    </article>
  );
}
