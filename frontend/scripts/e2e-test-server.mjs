import { createServer } from "node:http";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const host = "127.0.0.1";
const port = 4178;
const expectedHost = `${host}:${port}`;

export const harnessPage = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Test harness only</title>
  </head>
  <body>
    <main data-purpose="harness-only">
      <h1>Synthetic browser test surface</h1>
      <p id="identity-result">Loading synthetic identity.</p>
      <p id="api-result">Loading synthetic API response.</p>
      <output id="harness-ready" data-ready="false">Loading.</output>
    </main>
    <script>
      const identityResult = document.getElementById("identity-result");
      const apiResult = document.getElementById("api-result");
      const ready = document.getElementById("harness-ready");
      Promise.all([
        fetch("/api/test-identity", { cache: "no-store" }).then((response) => response.json()),
        fetch("/api/test-data", { cache: "no-store" }).then((response) => response.json()),
      ]).then(([identity, api]) => {
        identityResult.textContent = identity.kind === "synthetic" ? "Identity source: synthetic." : "Identity unavailable.";
        apiResult.textContent = api.kind === "synthetic" ? "API source: synthetic." : "API unavailable.";
        ready.dataset.ready = identity.kind === "synthetic" && api.kind === "synthetic" ? "true" : "false";
        ready.textContent = ready.dataset.ready === "true" ? "Ready." : "Unavailable.";
      }).catch(() => {
        ready.textContent = "Unavailable.";
      });
    </script>
  </body>
</html>`;

export function responseForTestRequest(method, requestUrl) {
  if (method !== "GET") {
    return { status: 405, contentType: "text/plain; charset=utf-8", body: "Method not allowed." };
  }

  if (requestUrl === "/test-harness") {
    return { status: 200, contentType: "text/html; charset=utf-8", body: harnessPage };
  }

  if (requestUrl === "/api/test-identity") {
    return {
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify({ kind: "synthetic", identity: "test-user" }),
    };
  }

  if (requestUrl === "/api/test-data") {
    return {
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify({ kind: "synthetic", available: true }),
    };
  }

  if (requestUrl === "/test-redirect") {
    return {
      status: 302,
      contentType: "text/plain; charset=utf-8",
      location: "https://example.invalid/blocked-test-redirect",
      body: "Redirects are denied by the test runner.",
    };
  }

  return { status: 404, contentType: "text/plain; charset=utf-8", body: "Not found." };
}

export function createTestServer() {
  return createServer((request, response) => {
    if (request.headers.host !== expectedHost) {
      response.writeHead(421, { "Cache-Control": "no-store" });
      response.end();
      return;
    }

    const result = responseForTestRequest(request.method ?? "", request.url ?? "");
    const headers = {
      "Cache-Control": "no-store",
      "Content-Type": result.contentType,
      "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self' http: https: ws: wss:; style-src 'none'; img-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    };
    if (result.location) headers.Location = result.location;

    response.writeHead(result.status, headers);
    response.end(result.body);
  });
}

const entryPath = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : "";
if (entryPath === import.meta.url) {
  const server = createTestServer();
  server.listen(port, host);
  server.on("error", () => {
    process.exitCode = 1;
  });
}
