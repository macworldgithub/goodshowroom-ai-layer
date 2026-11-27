import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeMarkdown, safeUrl } from "./safe-markdown";

function renderText(markdown: string) {
  return render(<SafeMarkdown>{markdown}</SafeMarkdown>).container;
}

/** No element may carry an event handler or a dangerous attribute. */
function noActiveContent(root: HTMLElement) {
  expect(root.querySelector("script,img,iframe,object,embed,style,form,input,button,svg,link,meta,base,video,audio,source,math")).toBeNull();
  for (const element of root.querySelectorAll("*")) {
    for (const attribute of element.attributes) {
      expect(attribute.name.startsWith("on"), `${element.tagName} ${attribute.name}`).toBe(false);
      expect(["style", "srcdoc", "src", "srcset", "formaction"]).not.toContain(attribute.name);
    }
  }
}

describe("safe links", () => {
  it("keeps only absolute https links without credentials", () => {
    expect(safeUrl("https://example.com/a?b=c#d")).toBe("https://example.com/a?b=c#d");
    for (const bad of [
      "javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=",
      "vbscript:msgbox(1)", "file:///etc/passwd", "blob:https://example.com/x", "http://insecure.example", "//evil.example/x", "/relative", "relative/path",
      "mailto:a@example.com", "tel:123", "https://user:pw@evil.example", "https://", "", "about:blank", "ftp://example.com",
    ]) expect(safeUrl(bad), JSON.stringify(bad)).toBeNull();
  });

  it("returns the cleaned address when the parser strips leading control characters", () => {
    expect(safeUrl("\u0000https://example.com")).toBe("https://example.com/");
    expect(safeUrl("\u0000javascript:alert(1)")).toBeNull();
  });

  it("opens a good link safely", () => {
    const root = renderText("[Docs](https://example.com/path)");
    const link = root.querySelector("a")!;
    expect(link.getAttribute("href")).toBe("https://example.com/path");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(link.textContent).toBe("Docs");
  });

  it("shows a blocked link as plain text, with no href", () => {
    for (const target of [
      "javascript:alert(1)", "JAVASCRIPT:alert(1)", "&#106;avascript:alert(1)", "javascript&colon;alert(1)", "data:text/html,x", "vbscript:x", "//evil.example",
      "/local", "http://insecure.example", "https://user:pw@evil.example", "<javascript:alert(1)>", "file:///x",
    ]) {
      const root = renderText(`[label](${target})`);
      expect(root.querySelector("a"), target).toBeNull();
      expect(root.textContent, target).toContain("label");
      noActiveContent(root);
    }
  });

  it("applies the same rule to autolinks and reference links", () => {
    const auto = renderText("<javascript:alert(1)> and <https://example.com/ok>");
    expect(auto.querySelectorAll("a")).toHaveLength(1);
    expect(auto.querySelector("a")!.getAttribute("href")).toBe("https://example.com/ok");
    const reference = renderText("[x][r]\n\n[r]: javascript:alert(1)");
    expect(reference.querySelector("a")).toBeNull();
  });
});

describe("hostile Markdown cannot execute or load anything", () => {
  const payloads = [
    "<script>window.__owned = 1</script>",
    "<img src=x onerror=\"window.__owned = 1\">",
    "<IMG SRC=\"https://evil.example/p.png\">",
    "![alt](https://evil.example/p.png)",
    "![alt](javascript:alert(1))",
    "<iframe src=\"https://evil.example\"></iframe>",
    "<svg onload=\"window.__owned = 1\"><circle/></svg>",
    "<object data=\"https://evil.example\"></object><embed src=\"https://evil.example\">",
    "<style>body{display:none}</style>",
    "<div style=\"position:fixed;inset:0\">cover</div>",
    "<form action=\"https://evil.example\"><input name=x></form>",
    "<a href=\"javascript:alert(1)\" onclick=\"x()\">raw</a>",
    "<meta http-equiv=\"refresh\" content=\"0;url=https://evil.example\"><base href=\"https://evil.example/\">",
    "<math><mi xlink:href=\"javascript:alert(1)\">x</mi></math>",
    "<video src=x onerror=alert(1)></video>",
    "<details open ontoggle=alert(1)>t</details>",
    "<!-- comment --><![CDATA[ x ]]>",
  ];

  it.each(payloads)("%s", (payload) => {
    const root = renderText(`Before\n\n${payload}\n\nAfter`);
    noActiveContent(root);
    expect((globalThis as { __owned?: number }).__owned).toBeUndefined();
    expect(root.querySelector("a[href^='javascript'], a[onclick]")).toBeNull();
    expect(root.textContent).toContain("Before");
    expect(root.textContent).toContain("After");
  });

  it("treats MDX and expressions as plain text", () => {
    const root = renderText("import Evil from \"./evil\"\n\nexport const x = 1\n\n{globalThis.__owned = 1}\n\n<Evil prop={1} />");
    expect((globalThis as { __owned?: number }).__owned).toBeUndefined();
    expect(root.textContent).toContain("{globalThis.__owned = 1}");
    noActiveContent(root);
  });

  it("shows escaped HTML as text and decodes nothing into markup", () => {
    const root = renderText("&lt;script&gt;alert(1)&lt;/script&gt; and `<img src=x onerror=alert(1)>`");
    expect(root.textContent).toContain("<script>alert(1)</script>");
    expect(root.textContent).toContain("<img src=x onerror=alert(1)>");
    noActiveContent(root);
  });

  it("allows only the approved elements", () => {
    const root = renderText("# Title\n\n## Sub\n\n**bold** _em_ `code`\n\n- one\n- two\n\n1. first\n\n> quote\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n~~gone~~ - [ ] task");
    const allowed = new Set(["DIV", "P", "STRONG", "EM", "CODE", "UL", "OL", "LI", "BLOCKQUOTE", "PRE", "BR", "A", "SPAN"]);
    for (const element of root.querySelectorAll("*")) expect(allowed.has(element.tagName), element.tagName).toBe(true);
    expect(root.querySelector("h1,h2,h3,table,input,del")).toBeNull();
    expect(root.textContent).toContain("Title");
  });
});
