import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { defineConfig } from "@playwright/test";

if (
  process.env.GOODSHOWROOM_PRIVACY_GUARD !== "active" ||
  process.env.GOODSHOWROOM_E2E_LAUNCHER !== "active" ||
  process.env.PLAYWRIGHT_NO_COPY_PROMPT !== "1"
) {
  throw new Error("Use the fixed npm browser test launcher.");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.mjs",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 15_000,
  expect: { timeout: 3_000 },
  globalSetup: "./scripts/e2e-global-setup.mjs",
  reporter: [["./e2e/safe-reporter.cjs"]],
  outputDir: resolve(tmpdir(), "goodshowroom-020-playwright-output"),
  preserveOutput: "never",
  use: {
    baseURL: "http://127.0.0.1:4178",
    browserName: "chromium",
    serviceWorkers: "block",
    acceptDownloads: false,
    screenshot: "off",
    video: "off",
    trace: "off",
    actionTimeout: 3_000,
    navigationTimeout: 5_000,
  },
});
