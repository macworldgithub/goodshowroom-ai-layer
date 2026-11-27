import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DatasetChart, MAX_CHART_ROWS, MIN_LINE_POINTS, type ChartProps } from "./dataset-chart";
import { DatasetTable, MAX_TABLE_ROWS } from "./dataset-table";

const columns: ChartProps["columns"] = [
  { key: "label", label: "Site", type: "string" },
  { key: "value", label: "Leads", type: "number", unit: "leads" },
];
const base = { width: 480, height: 240, categoryKey: "label", valueKey: "value", columns } as const;
const bars = (rows: ChartProps["rows"], title = "Leads by site"): ChartProps => ({ ...base, kind: "bar", title, rows });
const line = (rows: ChartProps["rows"]): ChartProps => ({ ...base, kind: "line", title: "Leads by day", rows });
const rows = [{ label: "North", value: 1200 }, { label: "South", value: 0 }, { label: "East", value: 42 }];

describe("bar charts", () => {
  it("draws a picture that is hidden from screen readers, with every value in a table", () => {
    const view = render(<DatasetChart {...bars(rows)} />);
    const figure = within(view.container).getByRole("figure", { name: "Leads by site" });
    expect(figure.textContent).toContain("Leads by site. 3 entries. The table below lists every value.");
    const picture = view.container.querySelector("[data-chart='bar']")!;
    expect(picture.getAttribute("aria-hidden")).toBe("true");
    expect(picture.querySelector("svg")).not.toBeNull();
    const table = within(figure).getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Leads (leads)" })).toBeTruthy();
    for (const [site, value] of [["North", "1,200"], ["South", "0"], ["East", "42"]]) {
      expect(within(table).getByRole("cell", { name: site })).toBeTruthy();
      expect(within(table).getAllByRole("cell", { name: value }).length).toBeGreaterThan(0);
    }
    expect(within(table).queryByText("No value")).toBeNull();
  });

  it("labels each bar with its value, not only a color", () => {
    const view = render(<DatasetChart {...bars(rows)} />);
    const labels = [...view.container.querySelectorAll("[data-chart='bar'] svg text")].map((node) => node.textContent);
    expect(labels).toEqual(expect.arrayContaining(["1,200", "0", "42"]));
  });
});

describe("line charts", () => {
  const days = [{ label: "Mon", value: 3 }, { label: "Tue", value: null }, { label: "Wed", value: 5 }, { label: "Thu", value: 0 }];

  it("keeps a missing value as a gap and never as zero", () => {
    const view = render(<DatasetChart {...line(days)} />);
    expect(view.container.querySelector("[data-chart='line'] svg")).not.toBeNull();
    const tuesday = within(view.container).getByRole("cell", { name: "Tue" }).parentElement!;
    expect(tuesday.textContent).toContain("No value");
    expect(tuesday.textContent).not.toMatch(/\b0\b/);
    expect(within(within(view.container).getByRole("cell", { name: "Thu" }).parentElement!).getByText("0")).toBeTruthy();
  });

  it("needs enough verified observations before it draws a line", () => {
    expect(MIN_LINE_POINTS).toBe(3);
    const few = render(<DatasetChart {...line([{ label: "Mon", value: 3 }, { label: "Tue", value: null }, { label: "Wed", value: 5 }])} />);
    expect(few.container.querySelector("[data-chart]")).toBeNull();
    expect(within(few.container).getByRole("note").textContent).toContain("not enough verified observations");
    expect(within(few.container).getByRole("table")).toBeTruthy();
  });
});

describe("hostile chart data", () => {
  const marker = "HOSTILE-LABEL-MARKER";
  const hostile = [
    `<img src=x onerror="window.__owned=1">${marker}`,
    `<script>window.__owned=1</script>${marker}`,
    `"><svg onload=window.__owned=1>${marker}`,
    `red;background:url(https://evil.example/x)}${marker}`,
    `</style><style>*{display:none}</style>${marker}`,
    `javascript:alert(1)${marker}`,
  ];

  it.each(hostile)("%s stays text", (label) => {
    const view = render(<DatasetChart {...bars([{ label, value: 1 }, { label: "Safe", value: 2 }], label)} />);
    expect((globalThis as { __owned?: number }).__owned).toBeUndefined();
    expect(view.container.querySelector("script,img,iframe,style,object,embed,foreignObject")).toBeNull();
    for (const element of view.container.querySelectorAll("*")) {
      for (const attribute of element.attributes) {
        // An escaped accessible name may hold the title. No other attribute may hold data.
        if (attribute.name !== "aria-label") expect(attribute.value.includes(marker), `${element.tagName} ${attribute.name}`).toBe(false);
        expect(attribute.name.startsWith("on")).toBe(false);
        expect(["srcdoc", "src", "href", "srcset"]).not.toContain(attribute.name);
      }
    }
    expect(view.container.textContent).toContain(marker);
  });

  it("falls back to the table alone for data it cannot chart", () => {
    const cases: ChartProps[] = [
      bars([]),
      bars([{ label: "A", value: Number.NaN }]),
      bars([{ label: "A", value: Number.POSITIVE_INFINITY }]),
      bars([{ label: "A", value: "5" as unknown as number }]),
      bars([{ label: 7 as unknown as string, value: 5 }]),
      { ...bars(rows), valueKey: "missing" },
      { ...bars(rows), categoryKey: "missing" },
      { ...bars(rows), columns: [columns[0], { key: "value", label: "Leads", type: "string" }] },
      bars(Array.from({ length: MAX_CHART_ROWS + 1 }, (_, index) => ({ label: `r${index}`, value: index }))),
    ];
    for (const props of cases) {
      const view = render(<DatasetChart {...props} />);
      expect(view.container.querySelector("svg"), props.title).toBeNull();
      expect(view.container.querySelector("table")).not.toBeNull();
      view.unmount();
    }
  });
});

describe("semantic tables", () => {
  it("shows exact values, labeled units and a keyboard-scrollable region", () => {
    const view = render(<DatasetTable caption="Leads by site" columns={columns} rows={rows} />);
    const region = within(view.container).getByRole("region", { name: "Leads by site" });
    expect(region.getAttribute("tabindex")).toBe("0");
    expect(within(region).getByRole("columnheader", { name: "Leads (leads)" }).getAttribute("scope")).toBe("col");
    expect(within(region).getByRole("cell", { name: "1,200" })).toBeTruthy();
  });

  it("shows No value instead of a number that is missing", () => {
    const view = render(<DatasetTable caption="t" columns={columns} rows={[{ label: "A", value: null }, { label: "B" }, { label: 3 as unknown as string, value: Number.NaN }]} />);
    expect(view.container.querySelectorAll("td")).toHaveLength(6);
    expect(within(view.container).getAllByText("No value")).toHaveLength(4);
    expect(view.container.textContent).not.toContain("NaN");
  });

  it("draws no more than its row bound and says so", () => {
    const many = Array.from({ length: MAX_TABLE_ROWS + 5 }, (_, index) => ({ label: `r${index}`, value: index }));
    const view = render(<DatasetTable caption="t" columns={columns} rows={many} />);
    expect(view.container.querySelectorAll("tbody tr")).toHaveLength(MAX_TABLE_ROWS);
    expect(view.container.textContent).toContain("Showing the first 200 rows.");
  });

  it("renders table text as text, never markup", () => {
    const view = render(<DatasetTable caption="<img src=x onerror=alert(1)>" columns={columns} rows={[{ label: "<script>alert(1)</script>", value: 1 }]} />);
    expect(view.container.querySelector("img,script")).toBeNull();
    expect(view.container.textContent).toContain("<script>alert(1)</script>");
  });
});
