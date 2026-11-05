import { afterEach, describe, expect, it, vi } from "vitest";
import { exportPKCS8, generateKeyPair, jwtVerify } from "jose";

vi.mock("server-only", () => ({}));
import { createLoginTransport } from "./login-transport";

const now = 1800000000;
const login = "22222222-2222-4222-8222-222222222222";
const user = "11111111-1111-4111-8111-111111111111";
const input = { purpose: "bootstrap" as const, provider: "google" as const, provider_account_id: "synthetic", email: "person@example.test", email_verified: true as const, auth_started_at: now, auth_expires_at: now + 28800, login_session_id: login };

afterEach(() => vi.unstubAllGlobals());

describe("server login transport", () => {
  it("signs an operation-bound exchange and rejects untrusted responses", async () => {
    const keys = await generateKeyPair("RS256", { modulusLength: 3072, extractable: true });
    const transport = createLoginTransport({ issuer: "issuer", audience: "backend", kid: "key", privatePem: await exportPKCS8(keys.privateKey), activatedAt: now - 1, revoked: false });
    const fetcher = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe("http://127.0.0.1:8000/v1/internal/login/exchange");
      const header = (init.headers as Record<string, string>).authorization;
      const token = header.slice(7);
      const verified = await jwtVerify(token, keys.publicKey, { issuer: "issuer", audience: "backend", currentDate: new Date(now * 1000) });
      expect(verified.payload.path).toBe("/v1/internal/login/exchange");
      expect(verified.payload.purpose).toBe("bootstrap");
      return Response.json({ schema_version: 1, data: { user_id: user, login_session_id: login, generation: 0, access_status: "pending" } });
    });
    vi.stubGlobal("fetch", fetcher);
    const binding = await transport.exchange(input, now);
    expect(binding).toEqual({ user_id: user, login_session_id: login, generation: 0, auth_started_at: now, auth_expires_at: now + 28800 });
    fetcher.mockImplementationOnce(async () => Response.json({ schema_version: 1, data: { user_id: user, login_session_id: "attacker", generation: 0, access_status: "approved" } }));
    await expect(transport.exchange(input, now)).rejects.toThrow("Login service unavailable.");
    fetcher.mockImplementationOnce(async () => new Response("x".repeat(8193), { status: 200 }));
    await expect(transport.exchange(input, now)).rejects.toThrow("Login service unavailable.");
    fetcher.mockImplementationOnce(async () => Response.json({ schema_version: 1, data: { lease_valid_until: new Date((now + 300) * 1000).toISOString() } }));
    await expect(transport.change(binding, "renew", now)).resolves.toBeUndefined();
    fetcher.mockImplementationOnce(async () => Response.json({ schema_version: 1, data: { lease_valid_until: new Date((now + 600) * 1000).toISOString() } }));
    await expect(transport.change(binding, "renew", now)).rejects.toThrow("Login service unavailable.");
    fetcher.mockImplementationOnce(async () => Response.json({ schema_version: 1 }));
    await expect(transport.change(binding, "revoke", now)).rejects.toThrow("Login service unavailable.");
    fetcher.mockImplementationOnce(async () => new Response(null, { status: 204 }));
    await expect(transport.change(binding, "revoke", now)).resolves.toBeUndefined();
  });
});
