import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import sharedFixtures from "../../../../contracts/fixtures.json";
import type { components } from "../../lib/api-contracts";
import { AnswerView } from "./answer-view";
import { ErrorNotice } from "./error-notice";
import { formatCount, formatPeriodDays, formatRetrieved } from "./format";

type Response = components["schemas"]["StructuredResponse"];
type ApiError = components["schemas"]["ApiError"];

const fixture = (name: string) => JSON.parse(sharedFixtures.cases.find((item) => item.name === name)!.json);
const zero: Response = fixture("saved_zero_result").data.response;
const partial: Response = fixture("temporary_partial_result").data.response;
const missing: Response = fixture("temporary_unavailable_notice").data.response;

/** A complete answer for any count. Every part uses the same value, as the backend does. */
function complete(value: number): Response {
  const copy = structuredClone(zero);
  copy.answer = `Total leads: ${value}.`;
  copy.claims[0].text = copy.answer;
  copy.facts[0].value = value;
  copy.datasets[0].rows[0].value = value;
  return copy;
}

function notice(status: Response["status"], answer: string, warning: Response["warnings"][number], coverage: Response["coverage"]["status"]): Response {
  return { ...structuredClone(missing), status, answer, warnings: [warning], coverage: { status: coverage, counts_disclosed: false } };
}
const unsupported = notice("unsupported", "This question is not supported yet.", "question_not_supported", "unknown");
const clarification = notice("clarification", "Which permitted dealership should I use?", "scope_required", "unknown");
const withheld = notice("unavailable", "This result cannot be shown under the current disclosure policy.", "disclosure_withheld", "suppressed");

function html(view: ReturnType<typeof render>) {
  return view.container.innerHTML;
}

describe("complete answers", () => {
  it("shows the same exact number in the text, the card and the table", () => {
    const view = render(<AnswerView response={complete(12345)} />);
    const article = view.container.querySelector("article")!;
    expect(article.getAttribute("data-state")).toBe("complete");
    // Once in the answer text, once in the closed evidence details.
    expect(within(article).getAllByText("Total leads: 12345.")).toHaveLength(2);
    const card = within(article).getByRole("group", { name: "Total leads: 12,345 leads" });
    expect(within(card).getByText("12,345")).toBeTruthy();
    expect(within(card).getByText("leads")).toBeTruthy();
    const table = within(article).getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Count (leads)" })).toBeTruthy();
    expect(within(table).getByRole("cell", { name: "12,345" })).toBeTruthy();
    expect(within(table).getByRole("cell", { name: "Total leads" })).toBeTruthy();
    expect(formatCount(12345)).toBe("12,345");
  });

  it("shows a verified zero as 0 in both visuals", () => {
    const view = render(<AnswerView response={zero} />);
    expect(zero.facts[0].value).toBe(0);
    const card = within(view.container).getByRole("group", { name: "Total leads: 0 leads" });
    expect(within(card).getByText("0")).toBeTruthy();
    expect(within(view.container).getByRole("cell", { name: "0" })).toBeTruthy();
    expect(within(view.container).queryByText("No value")).toBeNull();
  });

  it("shows period days, timezone, scope, source, retrieval time and coverage", () => {
    const view = render(<AnswerView response={complete(7)} />);
    const text = view.container.textContent ?? "";
    const period = zero.period!;
    expect(text).toContain(period.label);
    expect(text).toContain(formatPeriodDays(period.start_local, period.end_local));
    expect(text).toContain(period.timezone);
    expect(text).toContain(`${zero.scope!.site} (full permitted site)`);
    expect(text).toContain("Lead records");
    expect(text).toContain(formatRetrieved(zero.retrieved_at!, period.timezone));
    expect(view.container.querySelector("time")!.getAttribute("datetime")).toBe(zero.retrieved_at);
    expect(text).toContain("Coverage: complete");
  });

  it("offers closed evidence details and a snapshot note only when asked", () => {
    const view = render(<AnswerView response={complete(3)} snapshot />);
    const details = view.container.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("total_leads (version");
    expect(details.textContent).toContain("Reviewed metric definition");
    expect(view.container.textContent).toContain("This is a dated snapshot. Ask again to get fresh data.");
    const live = render(<AnswerView response={complete(3)} />);
    expect(live.container.textContent).not.toContain("dated snapshot");
  });

  it("never loads images, frames, scripts or forms", () => {
    const view = render(<AnswerView response={complete(5)} />);
    expect(view.container.querySelector("img,iframe,script,object,embed,form,input,link,style")).toBeNull();
  });
});

describe("every non-complete state is distinct and shows no business data", () => {
  const cases: Array<[string, Response, string, string]> = [
    ["partial", partial, "Partial result", "Coverage: partial"],
    ["unavailable", missing, "Data unavailable", "Coverage: unknown"],
    ["unsupported", unsupported, "Not supported yet", "Coverage: unknown"],
    ["clarification", clarification, "One detail is needed", "Coverage: unknown"],
    ["withheld", withheld, "Result withheld", "Coverage: withheld"],
  ];

  it.each(cases)("%s", (_name, response, title, coverage) => {
    const view = render(<AnswerView response={response} />);
    const status = within(view.container).getByRole("status");
    expect(within(status).getByText(title)).toBeTruthy();
    expect(status.textContent).toContain(response.answer);
    expect(view.container.textContent).toContain(coverage);
    expect(view.container.querySelector("table,figure,[role='group'],time,details")).toBeNull();
    expect(view.container.textContent).not.toMatch(/\d/);
  });

  it("uses a different title for each state", () => {
    const titles = cases.map(([, response]) => {
      const view = render(<AnswerView response={response} />);
      const title = view.container.querySelector("article")!.getAttribute("aria-label");
      view.unmount();
      return title;
    });
    expect(new Set(titles).size).toBe(cases.length);
  });
});

describe("answers that fail the approved rules show nothing from the payload", () => {
  const secret = "PRIVATE-PAYLOAD-MARKER";
  const bad = (label: string, change: (copy: Response) => void) => ({ label, change });
  const cases = [
    bad("card and table disagree with the fact", (copy) => { copy.datasets[0].rows[0].value = 99; }),
    bad("text disagrees with the fact", (copy) => { copy.answer = "Total leads: 99."; }),
    bad("unknown visual kind", (copy) => { (copy.visuals[0] as { kind: string }).kind = "script"; }),
    bad("an extra visual", (copy) => { copy.visuals.push({ ...copy.visuals[0], kind: "table" }); }),
    bad("markup in the text", (copy) => { copy.answer = `<script>${secret}</script>`; copy.claims[0].text = copy.answer; }),
    bad("changed source label", (copy) => { (copy.sources[0] as { label: string }).label = secret; }),
    bad("a different unit", (copy) => { (copy.datasets[0].columns[1] as { unit: string }).unit = secret; }),
    bad("period of the wrong length", (copy) => { copy.period!.end_local = copy.period!.start_local; }),
    bad("evidence that does not match the fact", (copy) => { copy.evidence[0].metric_version = 99; }),
    bad("retrieved before the period ended", (copy) => { copy.retrieved_at = copy.period!.start_utc; }),
  ];

  it.each(cases)("$label", ({ change }) => {
    const copy = complete(4);
    change(copy);
    const view = render(<AnswerView response={copy} />);
    const alert = within(view.container).getByRole("alert");
    expect(alert.getAttribute("data-state")).toBe("invalid");
    expect(view.container.textContent).toContain("This answer could not be validated");
    expect(view.container.textContent).not.toContain(secret);
    expect(view.container.textContent).not.toContain("Total leads");
    expect(view.container.querySelector("table,figure,time,script")).toBeNull();
  });

  it("refuses a notice that carries business data", () => {
    const view = render(<AnswerView response={{ ...structuredClone(missing), facts: structuredClone(zero.facts) }} />);
    expect(within(view.container).getByRole("alert").getAttribute("data-state")).toBe("invalid");
  });

  it("refuses a notice with changed wording", () => {
    const view = render(<AnswerView response={{ ...structuredClone(missing), answer: "[click](https://evil.example)" }} />);
    expect(html(view)).not.toContain("evil.example");
  });
});

describe("period and time formatting", () => {
  it("lists real calendar days, with the end day excluded", () => {
    expect(formatPeriodDays("2026-11-16T00:00:00+11:00", "2026-11-23T00:00:00+11:00")).toBe("16 Nov 2026 to 22 Nov 2026");
    expect(formatPeriodDays("2026-02-22T00:00:00+11:00", "2026-03-01T00:00:00+11:00")).toBe("22 Feb 2026 to 28 Feb 2026");
    expect(formatPeriodDays("2026-12-28T00:00:00+11:00", "2027-01-04T00:00:00+11:00")).toBe("28 Dec 2026 to 3 Jan 2027");
  });

  it("shows the retrieval time in the dealership timezone", () => {
    const shown = formatRetrieved("2026-11-23T00:05:00Z", "Australia/Melbourne");
    expect(shown).toContain("23 Nov 2026");
    expect(shown).toContain("11:05");
  });
});

describe("request errors", () => {
  const source: ApiError = fixture("source_unavailable_error").error;
  const unknown: ApiError = fixture("unknown_commit_error").error;

  it("shows the fixed message in an alert, with no result", () => {
    const view = render(<ErrorNotice error={source} />);
    const alert = within(view.container).getByRole("alert");
    expect(alert.getAttribute("data-code")).toBe("source_unavailable");
    expect(within(alert).getByText("Data unavailable")).toBeTruthy();
    expect(alert.textContent).toContain(source.message);
    expect(view.container.querySelector("table,figure,time")).toBeNull();
  });

  it("states save status only as far as the server confirmed it", () => {
    const sure = render(<ErrorNotice error={{ ...unknown, commit_state: "not_committed" }} />);
    expect(sure.container.textContent).toContain("Not saved.");
    const maybe = render(<ErrorNotice error={unknown} />);
    expect(maybe.container.textContent).toContain("Save status unknown.");
    expect(maybe.container.textContent).not.toContain("Not saved.");
    const none = render(<ErrorNotice error={{ ...source, commit_state: null }} />);
    expect(none.container.textContent).not.toMatch(/Not saved|Save status/);
  });

  it("offers a retry hint only for retryable errors", () => {
    const retry = render(<ErrorNotice error={{ ...source, retryable: true }} />);
    expect(retry.container.textContent).toContain("You can try again.");
    const fixed = render(<ErrorNotice error={{ ...source, retryable: false }} />);
    expect(fixed.container.textContent).not.toContain("You can try again.");
  });

  it("gives each error code its own title and survives an unknown code", () => {
    const codes = ["authentication_required", "session_expired", "approval_required", "access_denied", "not_found", "method_not_allowed", "version_conflict", "idempotency_conflict", "request_in_progress", "policy_changed", "temporary_unavailable", "payload_too_large", "invalid_request", "rate_limited", "model_failure", "response_invalid", "source_unavailable", "authorization_unavailable", "persistence_failed", "cleanup_pending", "deadline_exceeded", "internal_error"] as const;
    const titles = codes.map((code) => {
      const view = render(<ErrorNotice error={{ ...source, code }} />);
      const title = view.container.querySelector("p")!.textContent;
      view.unmount();
      return title;
    });
    expect(new Set(titles).size).toBe(codes.length);
    const odd = render(<ErrorNotice error={{ ...source, code: "made_up" as ApiError["code"] }} />);
    expect(odd.container.textContent).toContain("Something went wrong");
  });
});
