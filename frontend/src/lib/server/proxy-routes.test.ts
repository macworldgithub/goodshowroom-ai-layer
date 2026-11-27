import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
import { PROXY_ROUTES, matchRoute, queryAllowed } from "./proxy-routes";
import { csrfToken, validCsrf } from "./csrf";
import { validBrowserRequest } from "./browser-request";

const id = "11111111-1111-4111-8111-111111111111";
const other = "22222222-2222-4222-8222-222222222222";

describe("fixed proxy route table", () => {
  it("maps each allowed browser path to its backend path", () => {
    expect(matchRoute("GET", "/api/v1/me")).toMatchObject({ kind: "route", backendPath: "/v1/me" });
    expect(matchRoute("POST", `/api/v1/saved-chats/${id}/turns`)).toMatchObject({ kind: "route", backendPath: `/v1/saved-chats/${id}/turns` });
    expect(matchRoute("GET", `/api/v1/saved-chats/${id}/requests/${other}`)).toMatchObject({ kind: "route" });
    expect(matchRoute("PUT", `/api/v1/admin/users/${id}/identity-mappings/byd-leads-new`)).toMatchObject({ kind: "route" });
  });

  it("refuses every path that is not in the table", () => {
    const bad = [
      "/api/v1", "/api/v1/", "/api/v1/me/", "/api/v1//me", "/api/v1/unknown",
      "/api/v1/internal/login/exchange", "/api/v1/internal/login/renew", "/api/v1/internal/login/revoke",
      "/api/v1/me%2F..%2Fadmin%2Fusers", "/api/v1/me%00", "/api/v1/%2e%2e/me", "/api/v1/me\\x",
      "/api/v1/saved-chats/not-a-uuid/turns", `/api/v1/saved-chats/${"abcdefab-cdef-4abc-8def-abcdefabcdef".toUpperCase()}/turns`,
      `/api/v1/saved-chats/${id}/turns/extra`, "/api/v1/admin/users/x/access", "/api/auth/session", "/v1/me",
      "/api/v1/admin/users/" + id + "/identity-mappings/Bad_Slug", "/api/v1/admin/users/" + id + "/identity-mappings/",
      "/api/v1/https://evil.example/x", "/api/v1/me?x=1",
    ];
    for (const path of bad) expect(matchRoute("GET", path).kind, path).toBe("not_found");
  });

  it("separates unknown paths from wrong methods", () => {
    expect(matchRoute("DELETE", "/api/v1/me").kind).toBe("method_not_allowed");
    expect(matchRoute("PUT", "/api/v1/saved-chats").kind).toBe("method_not_allowed");
    expect(matchRoute("OPTIONS", "/api/v1/me").kind).toBe("method_not_allowed");
    expect(matchRoute("TRACE", "/api/v1/me").kind).toBe("method_not_allowed");
  });

  it("never lists an internal login route or a duplicate operation", () => {
    const keys = PROXY_ROUTES.map((route) => `${route.method} ${route.template}`);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys.some((key) => key.includes("internal"))).toBe(false);
  });

  it("allows only the documented query keys, once each", () => {
    const route = (path: string) => (matchRoute("GET", path) as { route: (typeof PROXY_ROUTES)[number] }).route;
    const list = route("/api/v1/saved-chats");
    expect(queryAllowed(list, "")).toBe(true);
    expect(queryAllowed(list, "?page_size=10&cursor=abc_-")).toBe(true);
    for (const bad of ["?", "?page_size", "?page_size=1&page_size=2", "?user_id=1", "?page_size=1&", "?a=b%00", "?page_size=é", "?page_size=1 2", "page_size=1"]) {
      expect(queryAllowed(list, bad), bad).toBe(false);
    }
    expect(queryAllowed(list, `?cursor=${"a".repeat(4601)}`)).toBe(false);
    expect(queryAllowed(route("/api/v1/me"), "?x=1")).toBe(false);
  });
});

describe("session-bound CSRF token", () => {
  const secret = "s".repeat(40);
  it("binds the token to one login session and one secret", () => {
    const token = csrfToken(secret, id);
    expect(validCsrf(secret, id, token)).toBe(true);
    expect(validCsrf(secret, other, token)).toBe(false);
    expect(validCsrf("t".repeat(40), id, token)).toBe(false);
    for (const bad of [null, "", "x", token.slice(1), `${token}A`, token.replace(/.$/, token.endsWith("A") ? "B" : "A"), "!".repeat(43)]) {
      expect(validCsrf(secret, id, bad as string | null)).toBe(false);
    }
  });
});

describe("browser request origin checks", () => {
  const site = new URL("http://localhost:3000");
  const make = (method: string, headers: Record<string, string>) => new Request("http://localhost:3000/api/v1/me", { method, headers });
  it("requires the configured host, and an exact origin for mutations", () => {
    expect(validBrowserRequest(make("GET", { host: "localhost:3000" }), site)).toBe(true);
    expect(validBrowserRequest(make("GET", { host: "localhost:3000", origin: "http://localhost:3000", "sec-fetch-site": "same-origin" }), site)).toBe(true);
    expect(validBrowserRequest(make("POST", { host: "localhost:3000", origin: "http://localhost:3000" }), site)).toBe(true);
    expect(validBrowserRequest(make("POST", { host: "localhost:3000" }), site)).toBe(false);
    expect(validBrowserRequest(make("POST", { host: "localhost:3000", origin: "null" }), site)).toBe(false);
    expect(validBrowserRequest(make("DELETE", { host: "localhost:3000", origin: "http://localhost:3001" }), site)).toBe(false);
    expect(validBrowserRequest(make("PATCH", { host: "localhost:3000", origin: "https://localhost:3000" }), site)).toBe(false);
    expect(validBrowserRequest(make("GET", { host: "evil.example" }), site)).toBe(false);
    expect(validBrowserRequest(make("GET", { host: "localhost:3000", "x-forwarded-host": "evil.example" }), site)).toBe(false);
    expect(validBrowserRequest(make("GET", { host: "localhost:3000", "x-forwarded-proto": "https" }), site)).toBe(false);
    expect(validBrowserRequest(make("GET", { host: "localhost:3000", origin: "http://evil.example" }), site)).toBe(false);
    expect(validBrowserRequest(make("POST", { host: "localhost:3000", origin: "http://localhost:3000", "sec-fetch-site": "cross-site" }), site)).toBe(false);
    expect(validBrowserRequest(make("GET", { host: "localhost:3000", "sec-fetch-site": "same-site" }), site)).toBe(false);
  });
});
