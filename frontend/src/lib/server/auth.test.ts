import { afterEach, describe, expect, it, vi } from "vitest";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

vi.mock("server-only", () => ({}));
import { configuredAuthOptions, makeAuthOptions } from "./auth";
import { authRequestContext } from "./auth-logger";

const start = 1800000000;
const userId = "11111111-1111-4111-8111-111111111111";
const loginId = "22222222-2222-4222-8222-222222222222";
const profile = { sub: "synthetic-google-id", email: "employee@example.test", email_verified: true };
const account = { provider: "google", providerAccountId: "synthetic-google-id" };
const require = createRequire(import.meta.url);
const core = require(join(dirname(require.resolve("next-auth")), "core", "index.js"));

afterEach(() => vi.unstubAllEnvs());

function setup() {
  const transport = {
    exchange: vi.fn(async (input) => ({ user_id: userId, login_session_id: input.login_session_id, generation: 0, auth_started_at: input.auth_started_at, auth_expires_at: input.auth_expires_at })),
    change: vi.fn(async () => undefined),
  };
  const options = makeAuthOptions({ clientId: "synthetic", clientSecret: "synthetic", secret: "synthetic-test-only-secret-32-bytes-minimum", transport, now: () => start });
  return { options, transport };
}

describe("Google login callbacks", () => {
  it("requests consent again only when the isolated test enables it", () => {
    const { options, transport } = setup();
    const forced = makeAuthOptions({ clientId: "synthetic", clientSecret: "synthetic", secret: "synthetic-test-only-secret-32-bytes-minimum", transport, forceConsent: true });
    const providerOptions = (value: typeof options) => (value.providers[0] as { options: { authorization?: { params: { prompt: string } } } }).options;
    expect(providerOptions(options).authorization).toBeUndefined();
    expect(providerOptions(forced).authorization?.params.prompt).toBe("consent");
  });

  it.each([
    ["production", "http://localhost:3000", "true"],
    ["test", "https://example.test", "true"],
    ["test", "http://localhost:3000", "invalid"],
  ])("rejects unsafe consent-test configuration", (environment, url, setting) => {
    vi.stubEnv("APP_ENV", environment);
    vi.stubEnv("NEXTAUTH_URL", url);
    vi.stubEnv("GOOGLE_TEST_FORCE_CONSENT", setting);
    expect(() => configuredAuthOptions()).toThrow("Login configuration unavailable.");
  });

  it("rejects mismatched or unverified callback profiles", async () => {
    const { options, transport } = setup();
    const signIn = options.callbacks!.signIn!;
    expect(await signIn({ account, profile } as never)).toBe(true);
    expect(await signIn({ account, profile: { ...profile, email_verified: false } } as never)).toBe(false);
    expect(await signIn({ account: null, profile } as never)).toBe(false);
    expect(transport.exchange).not.toHaveBeenCalled();
  });

  it("binds a verified callback and keeps private binding out of the browser session", async () => {
    const { options, transport } = setup();
    const token = await options.callbacks!.jwt!({ token: { email: profile.email }, account, profile } as never);
    expect(transport.exchange).toHaveBeenCalledOnce();
    expect(token.binding).toMatchObject({ user_id: userId, generation: 0 });
    const session = await options.callbacks!.session!({ session: { user: { email: profile.email }, expires: "" }, token } as never);
    expect(JSON.stringify(session)).not.toContain(userId);
    expect(JSON.stringify(session)).not.toContain("binding");
    expect(session.expires).toBe(new Date((start + 28800) * 1000).toISOString());
    await options.callbacks!.jwt!({ token, trigger: "update", session: { binding: { user_id: loginId } } } as never);
    expect(transport.change).toHaveBeenCalledWith(token.binding, "renew", start);
    await options.events!.signOut!({ token } as never);
    expect(transport.change).toHaveBeenCalledWith(token.binding, "revoke", start);
  });

  it("fails closed on missing binding and service failure", async () => {
    const { options, transport } = setup();
    await expect(options.callbacks!.jwt!({ token: {} } as never)).rejects.toThrow();
    transport.exchange.mockRejectedValueOnce(new Error("synthetic failure"));
    await expect(options.callbacks!.jwt!({ token: {}, account, profile } as never)).rejects.toThrow();
    await expect(options.events!.signOut!({ token: {} } as never)).rejects.toThrow();
  });

  it("uses NextAuth to reject cancellation and sign-out without its CSRF token", async () => {
    const { options, transport } = setup();
    const req = { headers: { host: "localhost:3000" }, cookies: {}, query: {}, body: {} };
    const cancelled = await core.AuthHandler({ options, req: { ...req, action: "callback", providerId: "google", method: "GET", query: { error: "access_denied" } } });
    expect(cancelled.redirect).toContain("error=");
    expect(transport.exchange).not.toHaveBeenCalled();
    const denied = await core.AuthHandler({ options, req: { ...req, action: "signout", method: "POST" } });
    expect(denied.redirect).toContain("csrf=true");
    expect(transport.change).not.toHaveBeenCalled();
  });

  it("detects a NextAuth sign-out event failure after CSRF verification", async () => {
    const { options, transport } = setup();
    const req = { headers: { host: "localhost:3000" }, cookies: {}, query: {}, body: {} };
    const csrf = await core.AuthHandler({ options, req: { ...req, action: "csrf", method: "GET" } });
    const csrfCookie = csrf.cookies.find((cookie: { name: string }) => cookie.name === "next-auth.csrf-token");
    expect(csrfCookie).toBeDefined();
    const binding = { user_id: userId, login_session_id: loginId, generation: 0, auth_started_at: start, auth_expires_at: start + 28800 };
    options.jwt = { ...options.jwt, decode: async () => ({ binding }) };
    transport.change.mockRejectedValueOnce(new Error("synthetic cleanup failure"));
    const state = { logoutFailed: false };
    await authRequestContext.run(state, () => core.AuthHandler({ options, req: { ...req, action: "signout", method: "POST", body: { csrfToken: csrf.body.csrfToken }, cookies: { [csrfCookie.name]: csrfCookie.value, "next-auth.session-token": "synthetic-session" } } }));
    expect(transport.change).toHaveBeenCalledWith(binding, "revoke", start);
    expect(state.logoutFailed).toBe(true);
  });
});
