// @vitest-environment node
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
import { makeAuthOptions } from "./auth";

const require = createRequire(import.meta.url);
const core = require(join(dirname(require.resolve("next-auth")), "core", "index.js"));
const jwt = require("next-auth/jwt");
const openid = require("openid-client");
const secret = "synthetic-test-only-secret-32-bytes-minimum";
const userId = "11111111-1111-4111-8111-111111111111";
const profile = { sub: "synthetic-google", email: "person@example.test", email_verified: true, name: "Synthetic" };
const now = Math.floor(Date.now() / 1000);

afterEach(() => vi.restoreAllMocks());

describe("NextAuth Google callback", () => {
  it("uses state and PKCE cookies, exchanges identity, and does not retain provider tokens", async () => {
    const callback = vi.fn(async (_url, params, checks) => {
      expect(params.state).toBe("synthetic-state");
      expect(checks.state).toBe("synthetic-state");
      expect(checks.code_verifier).toBe("synthetic-verifier");
      return { access_token: "synthetic-provider-token", claims: () => profile };
    });
    class FakeGoogleClient {
      callback = callback;
      callbackParams() { return { code: "synthetic-code", state: "synthetic-state" }; }
    }
    vi.spyOn(openid.Issuer, "discover").mockResolvedValue({ Client: FakeGoogleClient });
    const transport = {
      exchange: vi.fn(async (input) => ({ user_id: userId, login_session_id: input.login_session_id, generation: 0, auth_started_at: input.auth_started_at, auth_expires_at: input.auth_expires_at })),
      change: vi.fn(async () => undefined),
    };
    const options = makeAuthOptions({ clientId: "synthetic", clientSecret: "synthetic", secret, transport, now: () => now });
    const stateName = "next-auth.state";
    const pkceName = "next-auth.pkce.code_verifier";
    const state = await jwt.encode({ secret, salt: stateName, token: { value: "synthetic-state", provider: "google" } });
    const pkce = await jwt.encode({ secret, salt: pkceName, token: { value: "synthetic-verifier", provider: "google" } });
    const response = await core.AuthHandler({ options, req: { headers: { host: "localhost:3000" }, cookies: { [stateName]: state, [pkceName]: pkce }, query: { code: "synthetic-code", state: "synthetic-state" }, body: {}, action: "callback", providerId: "google", method: "GET" } });
    expect(transport.exchange).toHaveBeenCalledOnce();
    expect(callback).toHaveBeenCalledOnce();
    const session = response.cookies.find((cookie: { name: string }) => cookie.name === "next-auth.session-token");
    expect(session.options.httpOnly).toBe(true);
    expect(session.options.sameSite).toBe("lax");
    const decrypted = await jwt.decode({ secret, token: session.value });
    expect(decrypted.binding.user_id).toBe(userId);
    expect(JSON.stringify(decrypted)).not.toContain("synthetic-provider-token");
  });

  it("rejects a callback missing state or PKCE cookies before exchange", async () => {
    class FakeGoogleClient {}
    vi.spyOn(openid.Issuer, "discover").mockResolvedValue({ Client: FakeGoogleClient });
    const transport = { exchange: vi.fn(), change: vi.fn() };
    const options = makeAuthOptions({ clientId: "synthetic", clientSecret: "synthetic", secret, transport, now: () => now });
    const stateName = "next-auth.state";
    const state = await jwt.encode({ secret, salt: stateName, token: { value: "synthetic-state", provider: "google" } });
    for (const cookies of [{}, { [stateName]: state }]) {
      const response = await core.AuthHandler({ options, req: { headers: { host: "localhost:3000" }, cookies, query: { code: "synthetic-code", state: "synthetic-state" }, body: {}, action: "callback", providerId: "google", method: "GET" } });
      expect(response.redirect).toContain("error=");
    }
    expect(transport.exchange).not.toHaveBeenCalled();
  });
});
