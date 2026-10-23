import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { createTestEnvironment } from "./test-environment.mjs";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
if (process.argv.length > 2) {
  process.stderr.write("Unit test option overrides are disabled.\n");
  process.exit(2);
}

const env = createTestEnvironment();
const require = createRequire(import.meta.url);
const vitestPackage = require.resolve("vitest/package.json");
const vitestCli = resolve(dirname(vitestPackage), "vitest.mjs");
const child = spawn(process.execPath, [vitestCli, "run", "--config", "vitest.config.ts"], {
  cwd: frontendRoot,
  env,
  stdio: "inherit",
  windowsHide: true,
});

child.on("error", () => {
  process.stderr.write("Unit test runner could not start.\n");
  process.exitCode = 1;
});
child.on("exit", (code, signal) => {
  process.exitCode = signal ? 1 : code ?? 1;
});
