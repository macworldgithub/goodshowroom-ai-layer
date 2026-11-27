// @vitest-environment node
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { exportPKCS8, exportSPKI, generateKeyPair, jwtVerify, type CryptoKey } from "jose";
import { encode } from "next-auth/jwt";

vi.mock("server-only", () => ({}));
import { configuredBackendProxy } from "./proxy-config";
import { csrfToken } from "./csrf";
import { operationDigest } from "./identity";

const SECRET = "synthetic-session-secret-with-more-than-32-bytes";
const SITE = "http://localhost:3000";
const user = "11111111-1111-4111-8111-111111111111";
const chat = "33333333-3333-4333-8333-333333333333";
let publicKey: CryptoKey;
let counter = 0;

beforeAll(async () => {
  const keys = await generateKeyPair("RS256", { modulusLength: 3072, extractable: true });
  publicKey = keys.publicKey;
  vi.stubEnv("AUTH_SECRET", SECRET);
  vi.stubEnv("NEXTAUTH_URL", SITE);
  vi.stubEnv("IDENTITY_ISSUER", "issuer");
  vi.stubEnv("IDENTITY_AUDIENCE", "backend");
  vi.stubEnv("IDENTITY_KEY_ID", "key");
  vi.stubEnv("IDENTITY_KEY_ACTIVATED_AT", String(Math.floor(Date.now() / 1000) - 60));
  vi.stubEnv("IDENTITY_PRIVATE_KEY", await exportPKCS8(keys.privateKey));
  void (await exportSPKI(keys.publicKey));
});
afterEach(() => vi.unstubAllGlobals());

/** A real NextAuth-encrypted session cookie for a fresh login. */
async function login(overrides: Partial<Record<string, unknown>> = {}) {
  const time = Math.floor(Date.now() / 1000);
  const binding = { user_id: user, login_session_id: `44444444-4444-4444-8444-${String(++counter).padStart(12, "0")}`, generation: 2, auth_started_at: time - 10, auth_expires_at: time + 3600, ...overrides };
  const cookie = `next-auth.session-token=${await encode({ token: { binding }, secret: SECRET })}`;
  return { binding, cookie, csrf: csrfToken(SECRET, binding.login_session_id) };
}

type Seen = { url: string; init: RequestInit };
function backend(respond: (url: string, init: RequestInit) => Response | Promise<Response> = () => Response.json({ schema_version: 1, data: { ok: true } })) {
  const seen: Seen[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    seen.push({ url, init });
    if (url.endsWith("/v1/internal/login/renew")) return Response.json({ schema_version: 1, data: { lease_valid_until: new Date(Date.now() + 300_000).toISOString() } });
    return respond(url, init);
  });
  return seen;
}
const calls = (seen: Seen[]) => seen.filter((call) => !call.url.includes("/internal/"));

function call(path: string, init: RequestInit & { headers?: Record<string, string> } = {}) {
  const headers = { host: "localhost:3000", ...init.headers };
  return configuredBackendProxy()(new Request(`${SITE}${path}`, { ...init, headers }));
}

async function bearer(seen: Seen) {
  const header = (seen.init.headers as Record<string, string>).authorization;
  return (await jwtVerify(header.slice(7), publicKey, { issuer: "issuer", audience: "backend" })).payload;
}

describe("server-session injection and identity", () => {
  it("signs a backend assertion from the server session only", async () => {
    const seen = backend();
    const { cookie, binding } = await login();
    const response = await call("/api/v1/me", { headers: { cookie, "x-user-id": "attacker", "x-roles": "admin", "x-forwarded-user": "attacker" } });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ schema_version: 1, data: { ok: true } });
    const [forwarded] = calls(seen);
    expect(forwarded.url).toBe("http://127.0.0.1:8000/v1/me");
    const claims = await bearer(forwarded);
    expect(claims).toMatchObject({ purpose: "request", sub: user, generation: 2, login_session_id: binding.login_session_id, method: "GET", path: "/v1/me", digest: operationDigest("GET", "/v1/me") });
    const names = Object.keys(forwarded.init.headers as object).sort();
    expect(names).toEqual(["accept", "authorization"]);
    expect(forwarded.init.redirect).toBe("error");
  });

  it("renews the login lease first, then at most every four minutes", async () => {
    const seen = backend();
    const { cookie } = await login();
    await call("/api/v1/me", { headers: { cookie } });
    await call("/api/v1/me", { headers: { cookie } });
    expect(seen.filter((item) => item.url.endsWith("/internal/login/renew"))).toHaveLength(1);
    expect(calls(seen)).toHaveLength(2);
  });

  it("denies requests without a verified session before calling the backend", async () => {
    const seen = backend();
    const { cookie } = await login();
    const attempts: Array<[string, Record<string, string>]> = [
      ["no cookie", {}],
      ["forged cookie", { cookie: "next-auth.session-token=not-a-token" }],
      ["bearer header", { authorization: `Bearer ${cookie.split("=")[1]}` }],
      ["body cookie name", { cookie: `other=${cookie.split("=")[1]}` }],
      ["wrong secret", { cookie: `next-auth.session-token=${await encode({ token: { binding: (await login()).binding }, secret: "z".repeat(40) })}` }],
    ];
    for (const [label, headers] of attempts) {
      const response = await call("/api/v1/me", { headers });
      expect(response.status, label).toBe(401);
      expect((await response.json()).error.code).toBe("authentication_required");
    }
    for (const bad of [{ auth_expires_at: 1 }, { auth_started_at: Math.floor(Date.now() / 1000) + 600 }, { generation: -1 }, { user_id: "not-a-uuid" }]) {
      expect((await call("/api/v1/me", { headers: { cookie: (await login(bad)).cookie } })).status).toBe(401);
    }
    expect(seen).toHaveLength(0);
  });

  it("fails closed when the proxy is not configured", async () => {
    vi.stubEnv("AUTH_SECRET", "short");
    expect(() => configuredBackendProxy()).toThrow("Proxy configuration unavailable.");
    vi.stubEnv("AUTH_SECRET", SECRET);
  });
});

describe("origin, host and CSRF", () => {
  const body = JSON.stringify({ request_id: chat });
  const json = { "content-type": "application/json", origin: SITE };

  it("forwards a mutation only with exact origin and the session CSRF token", async () => {
    const seen = backend(() => Response.json({ schema_version: 1, data: { created: true } }, { status: 201 }));
    const { cookie, csrf, binding } = await login();
    const response = await call("/api/v1/saved-chats", { method: "POST", body, headers: { ...json, cookie, "x-csrf-token": csrf } });
    expect(response.status).toBe(201);
    const [forwarded] = calls(seen);
    expect(forwarded.init.method).toBe("POST");
    expect(Buffer.from(forwarded.init.body as Uint8Array).toString()).toBe(body);
    expect((forwarded.init.headers as Record<string, string>)["content-type"]).toBe("application/json");
    const claims = await bearer(forwarded);
    expect(claims.digest).toBe(operationDigest("POST", "/v1/saved-chats", new Uint8Array(), new TextEncoder().encode(body)));
    expect(claims.login_session_id).toBe(binding.login_session_id);
  });

  it("refuses forged or missing CSRF, origin and host on mutations", async () => {
    const seen = backend();
    const first = await login();
    const second = await login();
    const mutate = (headers: Record<string, string>) => call("/api/v1/saved-chats", { method: "POST", body, headers: { ...json, cookie: first.cookie, "x-csrf-token": first.csrf, ...headers } });
    expect((await mutate({ "x-csrf-token": "" })).status).toBe(403);
    expect((await mutate({ "x-csrf-token": second.csrf })).status).toBe(403);
    expect((await mutate({ "x-csrf-token": first.csrf.slice(0, -1) + (first.csrf.endsWith("A") ? "B" : "A") })).status).toBe(403);
    expect((await mutate({ origin: "http://evil.example" })).status).toBe(403);
    expect((await mutate({ origin: "null" })).status).toBe(403);
    expect((await mutate({ host: "evil.example" })).status).toBe(403);
    expect((await mutate({ "x-forwarded-host": "evil.example" })).status).toBe(403);
    expect((await mutate({ "sec-fetch-site": "cross-site" })).status).toBe(403);
    const noOrigin = await call("/api/v1/saved-chats", { method: "POST", body, headers: { "content-type": "application/json", cookie: first.cookie, "x-csrf-token": first.csrf } });
    expect(noOrigin.status).toBe(403);
    expect(calls(seen)).toHaveLength(0);
  });

  it("applies CSRF to every mutation method but not to reads", async () => {
    const seen = backend();
    const { cookie } = await login();
    for (const [method, path, requestBody] of [["DELETE", `/api/v1/saved-chats/${chat}`, undefined], ["PATCH", "/api/v1/admin/settings", "{}"], ["PUT", `/api/v1/admin/users/${user}/identity-mappings/byd-panel`, "{}"], ["POST", "/api/v1/access-requests", undefined]] as const) {
      const response = await call(path, { method, body: requestBody, headers: { cookie, origin: SITE, "content-type": requestBody ? "application/json" : "" } });
      expect(response.status, `${method} ${path}`).toBe(403);
    }
    expect((await call("/api/v1/me", { headers: { cookie } })).status).toBe(200);
    expect(calls(seen)).toHaveLength(1);
  });

  it("issues a CSRF token only to a valid session", async () => {
    backend();
    expect((await call("/api/v1/csrf")).status).toBe(401);
    const { cookie, csrf } = await login();
    const response = await call("/api/v1/csrf", { headers: { cookie } });
    expect(await response.json()).toMatchObject({ schema_version: 1, data: { csrf_token: csrf } });
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect((await call("/api/v1/csrf?x=1", { headers: { cookie } })).status).toBe(422);
    expect((await call("/api/v1/csrf", { method: "POST", headers: { cookie, origin: SITE } })).status).toBe(404);
  });
});

describe("proxy escape and malformed requests", () => {
  it("refuses unknown, internal, encoded and traversal paths without a backend call", async () => {
    const seen = backend();
    const { cookie, csrf } = await login();
    const headers = { cookie, origin: SITE, "x-csrf-token": csrf };
    for (const path of ["/api/v1/internal/login/revoke", "/api/v1/nothing", `/api/v1/saved-chats/${chat}/turns/x`, "/api/v1/me%2F..%2Fadmin%2Fusers", "/api/v1/saved-chats/%2e%2e", "/api/v1//me"]) {
      expect((await call(path, { method: "POST", body: "{}", headers: { ...headers, "content-type": "application/json" } })).status, path).toBe(404);
    }
    expect((await call("/api/v1/me", { method: "DELETE", headers })).status).toBe(405);
    expect(calls(seen)).toHaveLength(0);
  });

  it("rejects bad query strings, bodies and content types", async () => {
    const seen = backend();
    const { cookie, csrf } = await login();
    const base = { cookie, origin: SITE, "x-csrf-token": csrf };
    expect((await call("/api/v1/me?role=admin", { headers: base })).status).toBe(422);
    expect((await call("/api/v1/saved-chats?page_size=1&page_size=2", { headers: base })).status).toBe(422);
    expect((await call("/api/v1/saved-chats", { method: "POST", body: "{}", headers: { ...base, "content-type": "text/plain" } })).status).toBe(422);
    expect((await call("/api/v1/saved-chats", { method: "POST", body: "", headers: { ...base, "content-type": "application/json" } })).status).toBe(422);
    expect((await call("/api/v1/access-requests", { method: "POST", body: "{}", headers: { ...base, "content-type": "application/json" } })).status).toBe(422);
    const big = await call("/api/v1/saved-chats", { method: "POST", body: "x".repeat(60_001), headers: { ...base, "content-type": "application/json" } });
    expect(big.status).toBe(413);
    expect(calls(seen)).toHaveLength(0);
  });

  it("allows a documented query and sends it unchanged", async () => {
    const seen = backend();
    const { cookie } = await login();
    await call(`/api/v1/admin/conversations?user_id=${user}&page_size=5&since=2026-01-01&until=2026-02-01`, { headers: { cookie } });
    const [forwarded] = calls(seen);
    expect(forwarded.url).toBe(`http://127.0.0.1:8000/v1/admin/conversations?user_id=${user}&page_size=5&since=2026-01-01&until=2026-02-01`);
    const claims = await bearer(forwarded);
    expect(claims.digest).toBe(operationDigest("GET", "/v1/admin/conversations", new TextEncoder().encode(`user_id=${user}&page_size=5&since=2026-01-01&until=2026-02-01`)));
  });
});

describe("protected responses", () => {
  const get = async (respond: (url: string, init: RequestInit) => Response | Promise<Response>) => {
    backend(respond);
    const { cookie } = await login();
    return call("/api/v1/me", { headers: { cookie } });
  };

  it("returns no-store JSON and drops backend headers and cookies", async () => {
    const response = await get(() => Response.json({ schema_version: 1, data: {} }, { headers: { "set-cookie": "a=b", server: "uvicorn", "x-powered-by": "x", location: "http://evil.example" } }));
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    for (const name of ["set-cookie", "server", "x-powered-by", "location"]) expect(response.headers.has(name)).toBe(false);
  });

  it("passes safe error envelopes and the retry delay of a rate limit", async () => {
    const envelope = { schema_version: 1, request_id: chat, error: { code: "rate_limited", message: "Too many requests. Please wait before continuing.", retryable: true } };
    const response = await get(() => Response.json(envelope, { status: 429, headers: { "retry-after": "7" } }));
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("7");
    expect(await response.json()).toEqual(envelope);
  });

  it("replaces redirects, bad status, non-JSON, oversize and failures with a safe error", async () => {
    const secret = "internal-detail-token";
    const cases: Array<[string, () => Response | Promise<Response>]> = [
      ["redirect", () => { throw new TypeError(`redirect ${secret}`); }],
      ["status", () => new Response("{}", { status: 302, headers: { location: "http://evil.example" } })],
      ["html", () => new Response(`<html>${secret}</html>`, { headers: { "content-type": "text/html" } })],
      ["not json", () => new Response("nope", { headers: { "content-type": "application/json" } })],
      ["no version", () => Response.json({ data: {} })],
      ["oversize", () => new Response(JSON.stringify({ schema_version: 1, data: "x".repeat(1_048_600) }), { headers: { "content-type": "application/json" } })],
    ];
    for (const [label, respond] of cases) {
      const response = await get(respond);
      const text = await response.text();
      expect(response.status, label).toBe(503);
      expect(text, label).not.toContain(secret);
      expect(JSON.parse(text).error.code).toBe("source_unavailable");
      expect(response.headers.get("cache-control")).toBe("no-store");
    }
  });

  it("returns 204 without a body", async () => {
    const response = await get(() => new Response(null, { status: 204 }));
    expect(response.status).toBe(204);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("marks a failed mutation as unknown, and never leaks the assertion", async () => {
    backend(() => { throw Object.assign(new Error("boom"), { name: "TimeoutError" }); });
    const { cookie, csrf } = await login();
    const response = await call(`/api/v1/saved-chats/${chat}/turns`, { method: "POST", body: JSON.stringify({ request_id: chat }), headers: { cookie, origin: SITE, "x-csrf-token": csrf, "content-type": "application/json" } });
    const text = await response.text();
    expect(response.status).toBe(504);
    expect(JSON.parse(text).error).toMatchObject({ code: "deadline_exceeded", commit_state: "unknown" });
    expect(text).not.toMatch(/Bearer|eyJ|boom/);
  });

  it("denies access when the lease cannot be renewed", async () => {
    vi.stubGlobal("fetch", async () => new Response("down", { status: 500 }));
    const { cookie } = await login();
    const response = await call("/api/v1/me", { headers: { cookie } });
    expect(response.status).toBe(503);
    expect((await response.json()).error.code).toBe("authorization_unavailable");
  });
});
