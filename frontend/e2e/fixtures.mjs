import { test as base, expect } from "@playwright/test";
import { directRequestDenied, isAllowedHarnessRequest } from "../scripts/test-network-policy.mjs";

// Playwright fixture callbacks have a `use` argument that resembles a React hook.
/* eslint-disable react-hooks/rules-of-hooks */

const guardState = new WeakMap();
const apiMethods = ["delete", "fetch", "get", "head", "patch", "post", "put"];

export async function protectContext(context) {
  const state = { blockedRequests: 0, blockedWebSockets: 0 };
  guardState.set(context, state);

  for (const method of apiMethods) {
    Object.defineProperty(context.request, method, {
      configurable: true,
      value: directRequestDenied,
    });
  }

  await context.route("**/*", async (route) => {
    const request = route.request();
    if (!isAllowedHarnessRequest(request.url(), request.method())) {
      state.blockedRequests += 1;
      await route.abort("blockedbyclient").catch(() => {});
      return;
    }

    try {
      const response = await route.fetch({ maxRedirects: 0, maxRetries: 0, timeout: 1_500 });
      if (response.status() >= 300 && response.status() < 400) {
        state.blockedRequests += 1;
        await route.abort("blockedbyclient");
        return;
      }
      await route.fulfill({ response });
    } catch {
      state.blockedRequests += 1;
      await route.abort("blockedbyclient").catch(() => {});
    }
  });

  await context.routeWebSocket("**/*", async (webSocket) => {
    state.blockedWebSockets += 1;
    await webSocket.close({ code: 1008 });
  });
  return context;
}

export function getGuardState(context) {
  return guardState.get(context) ?? { blockedRequests: 0, blockedWebSockets: 0 };
}

const deniedApiRequest = Object.freeze(
  Object.fromEntries(apiMethods.map((method) => [method, directRequestDenied])),
);

export const test = base.extend({
  context: async ({ browser }, use) => {
    const context = await browser.newContext({
      serviceWorkers: "block",
      acceptDownloads: false,
    });
    await protectContext(context);
    try {
      await use(context);
    } finally {
      await context.close();
    }
  },
  request: async ({}, use) => {
    await use(deniedApiRequest);
  },
});

export { expect };
