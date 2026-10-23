import { test, expect, getGuardState, protectContext } from "./fixtures.mjs";

test.describe("Synthetic browser safety", () => {
  test("serves only a synthetic identity and API", async ({ page }) => {
    await page.goto("/test-harness");
    await expect(page.getByRole("heading", { name: "Synthetic browser test surface" })).toBeVisible();
    await expect(page.locator("#identity-result")).toHaveText("Identity source: synthetic.");
    await expect(page.locator("#api-result")).toHaveText("API source: synthetic.");
    await expect(page.locator("#harness-ready")).toHaveAttribute("data-ready", "true");
  });

  test("blocks external page requests", async ({ page, context }) => {
    await page.goto("/test-harness");
    const outcome = await page.evaluate(async () => {
      try {
        await fetch("https://example.invalid/blocked-request");
        return "allowed";
      } catch {
        return "blocked";
      }
    });
    expect(outcome).toBe("blocked");
    expect(getGuardState(context).blockedRequests).toBeGreaterThan(0);
  });

  test("blocks redirected local requests", async ({ page, context }) => {
    await page.goto("/test-harness");
    const outcome = await page.evaluate(async () => {
      try {
        const response = await fetch("/test-redirect", { cache: "no-store" });
        return response.redirected ? "redirected" : "returned";
      } catch {
        return "blocked";
      }
    });
    expect(outcome).toBe("blocked");
    expect(getGuardState(context).blockedRequests).toBeGreaterThan(0);
  });

  test("denies direct API request contexts", async ({ context, request }) => {
    await expect(context.request.get("https://example.invalid/blocked-api")).rejects.toThrow(
      "Direct API requests are disabled in offline tests.",
    );
    await expect(request.get("https://example.invalid/blocked-fixture")).rejects.toThrow(
      "Direct API requests are disabled in offline tests.",
    );
  });

  test("blocks WebSocket requests", async ({ page, context }) => {
    await page.goto("/test-harness");
    const outcome = await page.evaluate(
      () =>
        new Promise((resolve) => {
          const socket = new WebSocket("wss://example.invalid/blocked-socket");
          const timeout = window.setTimeout(() => resolve("timed-out"), 1_000);
          const finish = () => {
            window.clearTimeout(timeout);
            resolve("blocked");
          };
          socket.addEventListener("error", finish, { once: true });
          socket.addEventListener("close", finish, { once: true });
        }),
    );
    expect(outcome).toBe("blocked");
    expect(getGuardState(context).blockedWebSockets).toBeGreaterThan(0);
  });

  test("keeps browser contexts isolated", async ({ browser }) => {
    const first = await browser.newContext({ serviceWorkers: "block" });
    const second = await browser.newContext({ serviceWorkers: "block" });
    try {
      await protectContext(first);
      await protectContext(second);
      const firstPage = await first.newPage();
      await firstPage.goto("http://127.0.0.1:4178/test-harness");
      await firstPage.evaluate(() => localStorage.setItem("synthetic-isolation-check", "present"));

      const secondPage = await second.newPage();
      await secondPage.goto("http://127.0.0.1:4178/test-harness");
      const marker = await secondPage.evaluate(() => localStorage.getItem("synthetic-isolation-check"));
      expect(marker).toBeNull();
    } finally {
      await Promise.all([first.close(), second.close()]);
    }
  });
});
