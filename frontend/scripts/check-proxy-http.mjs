// Real HTTP check of the browser proxy. It starts the built Next.js server and a
// stand-in backend on loopback, then sends requests with a real NextAuth session
// cookie. It uses synthetic keys and values only. Run `npm run build` first, and
// stop any local backend: the proxy's fixed target is 127.0.0.1:8000.
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { generateKeyPairSync } from "node:crypto";
import { createRequire } from "node:module";
import { setTimeout as delay } from "node:timers/promises";
const root = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(`${root}package.json`);
const { pathToFileURL } = await import("node:url");
const { encode } = await import(pathToFileURL(require.resolve("next-auth/jwt")).href);
const pem = generateKeyPairSync("rsa", { modulusLength: 3072 }).privateKey.export({ type: "pkcs8", format: "pem" });
const SECRET = "smoke-secret-with-more-than-32-bytes!!";
const user = "11111111-1111-4111-8111-111111111111", chat = "33333333-3333-4333-8333-333333333333";
const now = Math.floor(Date.now() / 1000);
const binding = { user_id: user, login_session_id: "44444444-4444-4444-8444-000000000001", generation: 0, auth_started_at: now - 5, auth_expires_at: now + 3600 };
const seen = [];
const backend = createServer((req, res) => {
  let body = ""; req.on("data", (c) => body += c);
  req.on("end", () => {
    seen.push({ method: req.method, url: req.url, headers: req.headers, body });
    res.setHeader("content-type", "application/json"); res.setHeader("set-cookie", "leak=1");
    if (req.url.endsWith("/renew")) return res.end(JSON.stringify({ schema_version: 1, data: { lease_valid_until: new Date(Date.now() + 300000).toISOString() } }));
    res.end(JSON.stringify({ schema_version: 1, data: { ok: true } }));
  });
});
await new Promise((resolve, reject) => {
  backend.once("error", () => reject(new Error("Port 8000 is busy. Stop the local backend first.")));
  backend.listen(8000, "127.0.0.1", resolve);
});
const env = { ...process.env, AUTH_SECRET: SECRET, NEXTAUTH_URL: "http://localhost:3000", IDENTITY_ISSUER: "issuer", IDENTITY_AUDIENCE: "backend", IDENTITY_KEY_ID: "key", IDENTITY_KEY_ACTIVATED_AT: String(now - 60), IDENTITY_PRIVATE_KEY: pem.replace(/\n/g, "\n"), NEXT_TELEMETRY_DISABLED: "1" };
const next = spawn(process.execPath, ["node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", "3000"], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
let logs = ""; next.stdout.on("data", (d) => logs += d); next.stderr.on("data", (d) => logs += d);
const results = [];
const check = (name, ok, extra = "") => { results.push(ok); console.log(ok ? "PASS" : "FAIL", name, extra); };
try {
  for (let i = 0; i < 100; i++) { try { await fetch("http://localhost:3000/", { signal: AbortSignal.timeout(1000) }); break; } catch { await delay(200); } }
  const cookie = `next-auth.session-token=${await encode({ token: { binding }, secret: SECRET })}`;
  const site = "http://localhost:3000";
  const home = await fetch(site + "/");
  check("home CSP+frame", (home.headers.get("content-security-policy") || "").includes("frame-ancestors 'none'") && home.headers.get("x-frame-options") === "DENY");
  let r = await fetch(site + "/api/v1/me");
  check("no session -> 401 no-store", r.status === 401 && r.headers.get("cache-control") === "no-store", r.status);
  r = await fetch(site + "/api/v1/me", { headers: { cookie } });
  check("session GET /me -> 200", r.status === 200 && r.headers.get("cache-control") === "no-store" && !r.headers.has("set-cookie"), r.status);
  const fwd = seen.find((s) => s.url === "/v1/me");
  check("backend got bearer, no cookie", !!fwd && /^Bearer ey/.test(fwd.headers.authorization) && !fwd.headers.cookie);
  r = await fetch(site + "/api/v1/saved-chats", { method: "POST", headers: { cookie, origin: site, "content-type": "application/json" }, body: JSON.stringify({ request_id: chat }) });
  check("POST without csrf -> 403", r.status === 403, r.status);
  const token = (await (await fetch(site + "/api/v1/csrf", { headers: { cookie } })).json()).data.csrf_token;
  r = await fetch(site + "/api/v1/saved-chats", { method: "POST", headers: { cookie, origin: "http://evil.example", "x-csrf-token": token, "content-type": "application/json" }, body: JSON.stringify({ request_id: chat }) });
  check("POST evil origin -> 403", r.status === 403, r.status);
  r = await fetch(site + "/api/v1/saved-chats", { method: "POST", headers: { cookie, origin: site, "x-csrf-token": token, "content-type": "application/json" }, body: JSON.stringify({ request_id: chat }) });
  check("POST good -> 200", r.status === 200, r.status);
  const posted = seen.find((s) => s.method === "POST" && s.url === "/v1/saved-chats");
  check("backend got exact body", posted?.body === JSON.stringify({ request_id: chat }));
  r = await fetch(site + "/api/v1/temporary-chats", { method: "POST", headers: { cookie, origin: site, "x-csrf-token": token, "content-type": "application/json" }, body: JSON.stringify({ request_id: chat }) });
  check("temporary chat create -> 200", r.status === 200 && seen.some((s) => s.url === "/v1/temporary-chats"), r.status);
  r = await fetch(site + "/api/v1/temporary-chats", { headers: { cookie } });
  check("no temporary chat list route", r.status === 405, r.status);
  r = await fetch(site + "/api/v1/internal/login/revoke", { method: "POST", headers: { cookie, origin: site, "x-csrf-token": token } });
  check("internal route -> 404", r.status === 404, r.status);
  r = await fetch("http://127.0.0.1:3000/api/v1/me", { headers: { cookie } });
  check("wrong host -> 403", r.status === 403, r.status);
  r = await fetch(site + "/api/v1/me", { headers: { authorization: "Bearer " + cookie.split("=")[1] } });
  check("bearer-as-session -> 401", r.status === 401, r.status);
  await delay(300);
  const leaked = logs.includes("/api/v1") || logs.includes(chat) || logs.includes(token) || logs.includes(cookie.split("=")[1]);
  check("server log has no api paths/ids/tokens", !leaked);
  if (leaked) console.log(logs.slice(0, 800));
} finally { next.kill(); backend.close(); await delay(500); }
process.exit(results.every(Boolean) ? 0 : 1);
