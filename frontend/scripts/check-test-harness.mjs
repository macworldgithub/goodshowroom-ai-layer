import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, access, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import SafeVitestReporter from "./safe-vitest-reporter.mjs";
import { createTestEnvironment } from "./test-environment.mjs";
import { TEST_ORIGIN, directRequestDenied, isAllowedHarnessRequest } from "./test-network-policy.mjs";
import { responseForTestRequest } from "./e2e-test-server.mjs";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const privateSentinel = "SYNTHETIC_PRIVATE_DIAGNOSTIC_SENTINEL";
const output = [];
const originalWrite = process.stdout.write;
process.stdout.write = function captureSafeOutput(chunk, ...args) {
  output.push(Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk));
  const callback = args.find((value) => typeof value === "function");
  if (callback) process.nextTick(callback);
  return true;
};

try {
  const vitestReporter = new SafeVitestReporter();
  vitestReporter.onTestCaseResult({
    name: "renders the current local workspace page",
    result: () => ({ state: "failed", errors: [{ message: privateSentinel }] }),
  });
  vitestReporter.onTestRunEnd([], [{ message: privateSentinel }], "failed");

  const require = createRequire(import.meta.url);
  const PlaywrightReporter = require("../e2e/safe-reporter.cjs");
  const playwrightReporter = new PlaywrightReporter();
  playwrightReporter.onTestEnd(
    { title: "serves only a synthetic identity and API" },
    { status: "failed", errors: [{ message: privateSentinel }] },
  );
  playwrightReporter.onStdOut(Buffer.from(privateSentinel));
  playwrightReporter.onStdErr(Buffer.from(privateSentinel));
  playwrightReporter.onError({ message: privateSentinel });
  playwrightReporter.onEnd({ status: "failed" });
} finally {
  process.stdout.write = originalWrite;
}

assert(!output.join("").includes(privateSentinel), "Safe reporters exposed private diagnostic content.");
assert(output.some((line) => line.includes("failed")), "Safe reporters did not preserve failure status.");
assert(isAllowedHarnessRequest(`${TEST_ORIGIN}/test-harness`), "The local synthetic page must be allowed.");
assert(!isAllowedHarnessRequest("https://example.invalid/synthetic"), "External requests must be denied.");
assert(!isAllowedHarnessRequest(`${TEST_ORIGIN}/test-harness?token=synthetic`), "Query data must be denied.");
assert(!isAllowedHarnessRequest(`${TEST_ORIGIN}/api/test-data`, "POST"), "Non-GET requests must be denied.");

const identityResponse = responseForTestRequest("GET", "/api/test-identity");
assert.equal(identityResponse.status, 200);
assert.equal(JSON.parse(identityResponse.body).kind, "synthetic");
await assert.rejects(directRequestDenied(), /Direct API requests are disabled in offline tests\./);

const scratchDirectory = await mkdtemp(join(tmpdir(), "goodshowroom-020-"));
const diagnosticPath = join(scratchDirectory, "error-context.md");
const childSource = `
  import fs from "node:fs/promises";
  import net from "node:net";
  import dns from "node:dns/promises";
const localAllowed = await new Promise((resolve) => {
  const socket = new net.Socket();
  const timer = setTimeout(() => { socket.destroy(); resolve(false); }, 1_500);
  socket.once("connect", () => { clearTimeout(timer); socket.destroy(); resolve(true); });
  socket.once("error", (error) => {
    clearTimeout(timer);
    resolve(error.code === "ECONNREFUSED");
  });
  socket.connect({ host: "127.0.0.1", port: 4178 });
});
  await fs.writeFile(process.env.GOODSHOWROOM_SENTINEL_PATH, "${privateSentinel}");
  const socketBlocked = await new Promise((resolve) => {
    const socket = net.createConnection({ host: "192.0.2.1", port: 80 });
    const timer = setTimeout(() => { socket.destroy(); resolve(false); }, 1500);
    timer.unref();
    socket.once("error", (error) => { clearTimeout(timer); resolve(error.code === "ERR_TEST_NETWORK_BLOCKED"); });
    socket.once("connect", () => { clearTimeout(timer); socket.destroy(); resolve(false); });
  });
  let fetchBlocked = false;
  try { await fetch("https://example.invalid/private-check"); }
  catch (error) { fetchBlocked = error.code === "ERR_TEST_NETWORK_BLOCKED"; }
  let dnsBlocked = false;
  try { await dns.resolve4("example.invalid"); }
  catch (error) { dnsBlocked = error.code === "ERR_TEST_NETWORK_BLOCKED"; }
  if (localAllowed && socketBlocked && fetchBlocked && dnsBlocked) {
    process.stdout.write("NETWORK_GUARD_PASSED\\n");
  } else {
    process.stdout.write("NETWORK_GUARD_CHECKS local=" + Number(localAllowed) + " tcp=" + Number(socketBlocked) + " fetch=" + Number(fetchBlocked) + " dns=" + Number(dnsBlocked) + "\\n");
    process.exitCode = 1;
  }
`;

try {
  const guardResult = spawnSync(
    process.execPath,
    ["--input-type=module", "--eval", childSource],
    {
      cwd: frontendRoot,
      encoding: "utf8",
      timeout: 8_000,
      windowsHide: true,
      env: createTestEnvironment({ GOODSHOWROOM_SENTINEL_PATH: diagnosticPath }),
    },
  );
  const stderrCode = ["EADDRINUSE", "EACCES", "ECONNREFUSED", "ERR_TEST_NETWORK_BLOCKED"]
    .find((code) => guardResult.stderr.includes(code)) ?? "CHILD_PROCESS_ERROR";
  assert.equal(
    guardResult.status,
    0,
    `Network privacy self-check did not pass: ${guardResult.stdout.trim() || stderrCode}`,
  );
  assert(guardResult.stdout.includes("NETWORK_GUARD_PASSED"), "Network privacy self-check did not report success.");
  assert(!`${guardResult.stdout}${guardResult.stderr}`.includes(privateSentinel), "Private sentinel reached child output.");
  let diagnosticExists = true;
  try {
    await access(diagnosticPath);
  } catch {
    diagnosticExists = false;
  }
  assert.equal(diagnosticExists, false, "error-context.md must not be written.");

  const runnerResult = spawnSync(
    process.execPath,
    [resolve(frontendRoot, "scripts/run-playwright.mjs"), "--reporter=html"],
    { cwd: frontendRoot, encoding: "utf8", timeout: 5_000, windowsHide: true, env: createTestEnvironment() },
  );
  assert.notEqual(runnerResult.status, 0, "Unsafe Playwright option override must fail.");
  assert(!`${runnerResult.stdout}${runnerResult.stderr}`.includes(privateSentinel), "Unsafe-option check exposed private content.");
} finally {
  await rm(scratchDirectory, { recursive: true, force: true });
}

process.stdout.write("Harness guard and reporter simulation self-checks passed.\n");
