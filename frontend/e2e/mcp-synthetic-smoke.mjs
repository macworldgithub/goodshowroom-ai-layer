/* eslint-disable @typescript-eslint/no-unused-expressions */
async (sharedPage) => {
  const origin = "http://127.0.0.1:4178";
  const browser = sharedPage.context().browser();
  if (!browser) return { status: "failed", checks: 0, surface: "synthetic" };

  const context = await browser.newContext({ serviceWorkers: "block", acceptDownloads: false });
  let blockedRequests = 0;
  let blockedWebSockets = 0;
  const pageHtml = `<!doctype html>
    <html lang="en"><head><meta charset="utf-8"><title>Synthetic browser test surface</title></head>
    <body><main><h1>Synthetic browser test surface</h1>
      <p id="identity-result">Loading synthetic identity.</p>
      <p id="api-result">Loading synthetic API response.</p>
      <output id="harness-ready" data-ready="false">Loading.</output>
    </main><script>
      Promise.all([
        fetch('/api/test-identity', { cache: 'no-store' }).then(response => response.json()),
        fetch('/api/test-data', { cache: 'no-store' }).then(response => response.json())
      ]).then(([identity, data]) => {
        document.querySelector('#identity-result').textContent = identity.kind === 'synthetic' ? 'Identity source: synthetic.' : 'Identity unavailable.';
        document.querySelector('#api-result').textContent = data.kind === 'synthetic' ? 'API source: synthetic.' : 'API unavailable.';
        const ready = identity.kind === 'synthetic' && data.kind === 'synthetic';
        document.querySelector('#harness-ready').dataset.ready = String(ready);
      }).catch(() => {});
    </script></body></html>`;
  const responses = new Map([
    ["/test-harness", { status: 200, contentType: "text/html; charset=utf-8", body: pageHtml }],
    ["/api/test-identity", { status: 200, contentType: "application/json; charset=utf-8", body: JSON.stringify({ kind: "synthetic", identity: "test-user" }) }],
    ["/api/test-data", { status: 200, contentType: "application/json; charset=utf-8", body: JSON.stringify({ kind: "synthetic", available: true }) }],
    ["/test-redirect", { status: 302, headers: { location: "https://example.invalid/blocked-redirect" }, body: "" }],
  ]);
  const isAllowed = (url, method) => {
    try {
      const parsed = new URL(url, origin);
      return method === "GET" && parsed.origin === origin && !parsed.username && !parsed.password && !parsed.search && !parsed.hash && responses.has(parsed.pathname);
    } catch {
      return false;
    }
  };

  try {
    await context.route("**/*", async (route) => {
      const request = route.request();
      if (!isAllowed(request.url(), request.method())) {
        blockedRequests += 1;
        await route.abort("blockedbyclient").catch(() => {});
        return;
      }
      const response = responses.get(new URL(request.url()).pathname);
      if (response.status >= 300 && response.status < 400) {
        await route.fulfill(response);
        return;
      }
      await route.fulfill({ ...response, body: response.body });
    });
    await context.routeWebSocket("**/*", async (socket) => {
      blockedWebSockets += 1;
      await socket.close({ code: 1008 });
    });

    const page = await context.newPage();
    await page.goto(`${origin}/test-harness`, { waitUntil: "domcontentloaded", timeout: 5_000 });
    await page.locator('#harness-ready[data-ready="true"]').waitFor({ state: "attached", timeout: 3_000 });
    const externalBlocked = await page.evaluate(async () => {
      try {
        await fetch("https://example.invalid/mcp-smoke");
        return false;
      } catch {
        return true;
      }
    });
    const redirectBlocked = await page.evaluate(async () => {
      try {
        await fetch("/test-redirect", { cache: "no-store" });
        return false;
      } catch {
        return true;
      }
    });
    const websocketBlocked = await page.evaluate(
      () =>
        new Promise((resolve) => {
          const socket = new WebSocket("wss://example.invalid/mcp-smoke");
          const timer = window.setTimeout(() => resolve(false), 1_000);
          const finish = () => {
            window.clearTimeout(timer);
            resolve(true);
          };
          socket.addEventListener("error", finish, { once: true });
          socket.addEventListener("close", finish, { once: true });
        }),
    );
    const passed = externalBlocked && redirectBlocked && websocketBlocked && blockedRequests >= 2 && blockedWebSockets === 1;
    return {
      status: passed ? "passed" : "failed",
      checks: 4,
      blockedRequests,
      blockedWebSockets,
      surface: "synthetic",
    };
  } catch {
    return { status: "failed", checks: 0, blockedRequests, blockedWebSockets, surface: "synthetic" };
  } finally {
    await context.close();
  }
}
