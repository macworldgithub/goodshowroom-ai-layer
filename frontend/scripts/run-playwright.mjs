import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { createTestEnvironment } from "./test-environment.mjs";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const requestedOptions = process.argv.slice(2);
if (requestedOptions.some((option) => option !== "--list") || requestedOptions.length > 1) {
  process.stderr.write("Playwright option overrides are disabled.\n");
  process.exit(2);
}

const env = {
  ...createTestEnvironment(),
  GOODSHOWROOM_E2E_LAUNCHER: "active",
  PLAYWRIGHT_NO_COPY_PROMPT: "1",
};
const require = createRequire(import.meta.url);
const playwrightCli = require.resolve("@playwright/test/cli");
const child = spawn(
  process.execPath,
  [playwrightCli, "test", "--config=playwright.config.mjs", ...requestedOptions],
  { cwd: frontendRoot, env, stdio: "inherit", windowsHide: true },
);

child.on("error", () => {
  process.stderr.write("Browser test runner could not start.\n");
  process.exitCode = 1;
});
child.on("exit", (code, signal) => {
  process.exitCode = signal ? 1 : code ?? 1;
});
