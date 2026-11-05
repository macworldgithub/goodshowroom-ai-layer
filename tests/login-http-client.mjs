import assert from "node:assert/strict";
import net from "node:net";
import dns from "node:dns";

// This standalone test can connect only to the service it starts itself.
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = typeof first === "object" ? first?.host : args[1];
  const port = typeof first === "object" ? first?.port : first;
  if (host !== "127.0.0.1" || Number(port) !== 8000) throw new Error("Test network denied");
  return Reflect.apply(connect, this, args);
};
for (const name of ["lookup", "resolve", "resolve4", "resolve6"]) {
  dns[name] = () => { throw new Error("Test DNS denied"); };
  if (name in dns.promises) dns.promises[name] = () => Promise.reject(new Error("Test DNS denied"));
}
const realFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = new URL(String(input));
  if (url.origin !== "http://127.0.0.1:8000" || url.username || url.password || !["/v1/internal/login/exchange", "/v1/internal/login/renew", "/v1/internal/login/revoke"].includes(url.pathname) || url.search || url.hash || init?.method !== "POST") throw new Error("Test network denied");
  return realFetch(input, { ...init, redirect: "error" });
};

let raw = "";
for await (const part of process.stdin) raw += part;
const { config, input, now } = JSON.parse(raw);
const { createLoginTransport } = await import("../frontend/src/lib/server/login-transport.ts");
const transport = createLoginTransport(config);
const binding = await transport.exchange(input, now);
assert.equal(binding.login_session_id, input.login_session_id);
assert.deepEqual(await transport.exchange(input, now), binding);
await transport.change(binding, "renew", now);
const invalid = await fetch("http://127.0.0.1:8000/v1/internal/login/renew", { method: "POST", headers: { authorization: "Bearer invalid" } });
assert.ok([401, 403].includes(invalid.status));
await assert.rejects(transport.change(binding, "renew", now), /Login service unavailable/);
await assert.rejects(transport.change(binding, "renew", now), /Login service unavailable/);
await assert.rejects(transport.change(binding, "revoke", now), /Login service unavailable/);
await transport.change(binding, "revoke", now);
await assert.rejects(transport.change(binding, "renew", now), /Login service unavailable/);
process.stdout.write("PASS: frontend HTTP exchange, renewal, invalid assertion, unavailable service, timeout and cleanup retry\n");
