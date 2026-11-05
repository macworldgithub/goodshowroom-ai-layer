import { describe, expect, it, vi } from "vitest";
import { createRequire } from "node:module";

vi.mock("server-only", () => ({}));

import { googleIdentityFromVerifiedCallback } from "./google-identity";

const require = createRequire(import.meta.url);
const googleProvider = require("next-auth/providers/google").default;

describe("Google callback identity validation", () => {
  const profile = { sub: "google-123", email: "person@example.test", email_verified: true };

  it("returns only identity fields for a matching verified profile", () => {
    expect(googleIdentityFromVerifiedCallback("google", "google-123", {
      ...profile, access_token: "synthetic-token",
    })).toEqual({ provider: "google", provider_account_id: "google-123", email: "person@example.test", email_verified: true });
  });

  it.each([
    ["other", "google-123", profile],
    ["google", "different", profile],
    ["google", "google-123", { ...profile, email_verified: false }],
    ["google", "google-123", { ...profile, email: "invalid" }],
    ["google", "google-123", null],
    ["google", "", profile],
    ["google", "google-123", { ...profile, email_verified: "true" }],
    ["google", "google-123", { ...profile, email: "person @example.test" }],
    ["google", "google-123", []],
  ])("rejects an invalid callback identity", (provider, account, input) => {
    expect(() => googleIdentityFromVerifiedCallback(provider, account, input)).toThrow("Identity verification failed.");
  });

  it("retains the installed Google provider's identity and callback protections", () => {
    const provider = googleProvider({ clientId: "synthetic", clientSecret: "synthetic" });
    expect(provider.id).toBe("google");
    expect(provider.idToken).toBe(true);
    expect(provider.checks).toEqual(["pkce", "state"]);
    expect(provider.authorization.params.scope).toBe("openid email profile");
    expect(provider.wellKnown).toBe("https://accounts.google.com/.well-known/openid-configuration");
  });
});
