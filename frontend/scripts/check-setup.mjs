// Setup-only checks. No model, database, or browser connection is required.
import assert from "node:assert/strict";
import { randomUUID, createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { createServer } from "node:net";
import { parseEnv } from "node:util";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

process.chdir(fileURLToPath(new URL("../", import.meta.url)));
const rootEnv = new URL("../../.env", import.meta.url);
const original = await readFile(rootEnv).catch((error) => {
  if (error.code !== "ENOENT") throw new Error("Cannot read setup configuration.");
  return Buffer.alloc(0);
});
const before = { ...process.env };
await import("../next.config.mjs");
const parsed = parseEnv(original.toString());
for (const [name, value] of Object.entries(parsed)) {
  assert.ok(process.env[name] === (before[name] ?? value), "Root environment loading failed");
}
const sentinel = randomUUID();
process.env.GOODSHOWROOM_SETUP_SENTINEL = sentinel;
process.env.NEXT_TELEMETRY_DISABLED = "1";
const protectedValues = [sentinel, ...Object.entries(parsed)
  .filter(([name, value]) => /SECRET|TOKEN|PASSWORD|KEY|URI|CLIENT_ID/.test(name) && value.length >= 12)
  .map(([, value]) => value)];

async function run(args) {
  const child = spawn(process.execPath, ["node_modules/next/dist/bin/next", ...args], { stdio: "inherit" });
  await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => code === 0 ? resolve() : reject(new Error("Next build failed")));
  });
}

function inspect(text) {
  assert.ok(protectedValues.every((value) => !text.includes(value)), "Private value found in browser output");
}

async function inspectDirectory(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = `${directory}/${entry.name}`;
    if (entry.isDirectory()) await inspectDirectory(path);
    else inspect(await readFile(path, "utf8"));
  }
}

await run(["build"]);
await inspectDirectory(".next/static");
const reservation = createServer();
await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
const port = reservation.address().port;
await new Promise((resolve) => reservation.close(resolve));
const server = spawn(process.execPath, ["node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", String(port)], { stdio: "ignore" });
try {
  let response;
  for (let attempt = 0; attempt < 100; attempt++) {
    assert.ok(server.exitCode === null, "Local server stopped early");
    try {
      response = await fetch(`http://127.0.0.1:${port}`, { signal: AbortSignal.timeout(1000) });
      break;
    } catch { await delay(200); }
  }
  assert.ok(response?.ok, "Local startup failed");
  const html = await response.text();
  assert.ok(html.includes("Good Showroom AI"), "Setup page missing");
  inspect(html);
  for (const path of ["/api/v1/chats", "/.env"]) {
    const missing = await fetch(`http://127.0.0.1:${port}${path}`);
    assert.equal(missing.status, 404, "Unexpected setup route available");
    inspect(await missing.text());
  }
} finally {
  server.kill();
  await new Promise((resolve) => server.once("exit", resolve));
}
const after = await readFile(rootEnv).catch(() => Buffer.alloc(0));
assert.equal(createHash("sha256").update(after).digest("hex"), createHash("sha256").update(original).digest("hex"), "Environment file changed");
console.log("Setup checks passed: root environment, build, browser assets, HTTP startup, unavailable routes, unchanged environment.");
