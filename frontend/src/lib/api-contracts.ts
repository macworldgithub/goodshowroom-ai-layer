/** Public wire validation. This does not grant access or prove a saved commit. */
import { visit } from "jsonc-parser";
import * as validators from "./generated/validators.cjs";
import meta from "./generated/contract-meta.json";
import type { components } from "./generated/api-types";

type Schemas = components["schemas"];
type ContractMap = Pick<Schemas,
  "CreateChatRequest" | "TurnRequest" | "ChatResultEnvelope" | "ErrorEnvelope"
>;
export type ContractName = keyof ContractMap;
export type { components } from "./generated/api-types";

export class ApiContractError extends Error {
  constructor() { super("Request or response is invalid."); this.name = "ApiContractError"; }
}

function invalid(): never { throw new ApiContractError(); }

function decode(raw: string, maximum: number): unknown {
  if (typeof raw !== "string" || raw.length > maximum
    || new TextEncoder().encode(raw).length > maximum) invalid();
  const stack: Array<Set<string> | null> = [];
  let nodes = 0;
  const node = () => {
    if (++nodes > meta.limits.nodes || stack.length > meta.limits.depth) invalid();
  };
  const begin = (object: boolean) => {
    node(); stack.push(object ? new Set() : null);
    if (stack.length > meta.limits.depth + 1) invalid();
  };
  // Microsoft's parser supplies strict token visits; JSON.parse remains the
  // decoder. Reject duplicate keys before JSON.parse could silently replace one.
  visit(raw, {
    onObjectBegin: () => begin(true), onArrayBegin: () => begin(false),
    onObjectEnd: () => { stack.pop(); }, onArrayEnd: () => { stack.pop(); },
    onObjectProperty: (key) => {
      const keys = stack.at(-1);
      if (!key.isWellFormed() || !keys || keys.has(key)) invalid();
      keys.add(key);
    },
    onLiteralValue: (value: unknown, offset, length) => {
      node();
      if (typeof value === "string" && !value.isWellFormed()) invalid();
      if (typeof value === "number" && (
        !Number.isSafeInteger(value) || /[.eE]/.test(raw.slice(offset, offset + length))
      )) invalid();
    },
    onError: invalid,
  }, { disallowComments: true, allowTrailingComma: false, allowEmptyContent: false });
  return JSON.parse(raw) as unknown;
}

function equal(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (left === null || right === null || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const a = Object.keys(left).sort(), b = Object.keys(right).sort();
  return a.length === b.length && a.every((key, index) => key === b[index]
    && equal((left as Record<string, unknown>)[key], (right as Record<string, unknown>)[key]));
}

function timestamps(value: unknown): void {
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    if (item !== null && ["start_local", "end_local", "start_utc", "end_utc", "retrieved_at"].includes(key)) {
      if (typeof item !== "string" || !Number.isFinite(Date.parse(item))) invalid();
      if (!["start_local", "end_local"].includes(key) && !/(Z|\+00:00)$/.test(item)) invalid();
    }
    timestamps(item);
  }
}

function periodValid(period: Schemas["AnswerPeriod"]): void {
  const start = Date.parse(period.start_utc), end = Date.parse(period.end_utc);
  if (end <= start) invalid();
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: period.timezone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  for (const [local, instant] of [[period.start_local, start], [period.end_local, end]] as const) {
    const parts = Object.fromEntries(formatter.formatToParts(new Date(instant)).map(({ type, value }) => [type, value]));
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    if (!local.startsWith(`${date}T00:00:00`) || Date.parse(local) !== instant
      || [parts.hour, parts.minute, parts.second].some((part) => part !== "00")
      || !/^\d{4}-\d{2}-\d{2}T00:00:00(?:\.0{1,6})?(Z|[+-]\d{2}:\d{2})$/.test(local)) invalid();
  }
  const days = (Date.parse(period.end_local.slice(0, 10)) - Date.parse(period.start_local.slice(0, 10))) / 86_400_000;
  if (days !== 7) invalid();
}

const notices = [
  ["partial", "The available data is incomplete.", "incomplete_data", "partial"],
  ["unavailable", "The required data is unavailable.", "missing_data", "unknown"],
  ["unsupported", "This question is not supported yet.", "question_not_supported", "unknown"],
  ["clarification", "Which permitted dealership should I use?", "scope_required", "unknown"],
  ["unavailable", "This result cannot be shown under the current disclosure policy.", "disclosure_withheld", "suppressed"],
] as const;

function responseValid(response: Schemas["StructuredResponse"]): void {
  if (response.status !== "complete") {
    if (!notices.some(([status, answer, warning, coverage]) => response.status === status
      && response.answer === answer && equal(response.warnings, [warning])
      && equal(response.coverage, { status: coverage, counts_disclosed: false }))
      || [response.facts, response.claims, response.datasets, response.visuals, response.sources, response.evidence].some((array) => array.length)
      || response.period !== null || response.scope !== null || response.retrieved_at !== null) invalid();
    return;
  }
  const fact = response.facts[0];
  if (!fact || response.facts.length !== 1 || !response.period || !response.scope || !response.retrieved_at) invalid();
  const answer = `Total leads: ${fact.value}.`;
  if (response.answer !== answer
    || !equal(response.claims, [{ text: answer, fact_ids: ["fact_total_leads"] }])
    || !equal(response.datasets, [{ dataset_id: "dataset_total_leads", columns: [
      { key: "label", label: "Metric", type: "string", unit: null },
      { key: "value", label: "Count", type: "number", unit: "leads" },
    ], rows: [{ label: "Total leads", value: fact.value, fact_id: "fact_total_leads" }], next_cursor: null }])
    || !equal(response.visuals, ["card", "table"].map((kind) => ({
      kind, dataset_id: "dataset_total_leads", field_bindings: { label: "label", value: "value" },
    })))
    || !equal(response.sources, [{ source_id: "source_leads", label: "Lead records" }])
    || !equal(response.evidence, [{ evidence_id: "evidence_total_leads", source_id: "source_leads",
      metric_id: "total_leads", metric_version: fact.metric_version, provenance: "reviewed_metric_definition" }])
    || !equal(response.coverage, { status: "complete", counts_disclosed: false })
    || response.warnings.length || Date.parse(response.retrieved_at) < Date.parse(response.period.end_utc)) invalid();
  periodValid(response.period);
}

/** Pass raw JSON, not a previously parsed value, to preserve duplicate/number checks.
 * A future HTTP adapter must also pass the actual response status.
 * No validation details, raw input, or generated-validator errors leave this API.
 */
export function parseApiContract<K extends ContractName>(name: K, raw: string, httpStatus?: number): ContractMap[K] {
  try {
    if (!Object.hasOwn(validators, name)) invalid();
    const maximum = name === "TurnRequest" || name === "CreateChatRequest" ? meta.limits.request_bytes : meta.limits.response_bytes;
    const decoded = decode(raw, maximum);
    if (!validators[name](decoded)) invalid();
    timestamps(decoded);
    if (name === "TurnRequest") {
      const request = decoded as Schemas["TurnRequest"];
      // Match Python str.strip rather than JavaScript's different trim set.
      if (!/[^\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(request.message)) invalid();
    } else if (name === "ChatResultEnvelope") {
      const data = (decoded as Schemas["ChatResultEnvelope"]).data;
      if (data.persistence !== (data.mode === "saved" ? "committed" : "not_saved")
        || httpStatus !== undefined && httpStatus !== 200) invalid();
      responseValid(data.response);
    } else if (name === "ErrorEnvelope") {
      const error = (decoded as Schemas["ErrorEnvelope"]).error;
      const expected = meta.errors[error.code];
      if (error.message !== expected.message || error.retryable !== expected.retryable
        || error.commit_state != null && !expected.allows_commit_state
        || httpStatus !== undefined && httpStatus !== expected.status) invalid();
    }
    return decoded as ContractMap[K];
  } catch { throw new ApiContractError(); }
}
