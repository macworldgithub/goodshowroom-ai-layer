import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
vi.mock("./auth", () => ({ configuredAuthOptions: () => ({}) }));
vi.mock("next-auth", () => ({ default: () => async (request: Request) => {
  if (new URL(request.url).searchParams.has("fail")) {
    const { authLogger } = await import("./auth-logger");
    authLogger.error("SIGN_OUT_EVENT_ERROR");
  }
  return new Response("ok", { status: 200, headers: { "set-cookie": "synthetic=cleared; Max-Age=0" } });
} }));
import { POST } from "../../app/api/auth/[...nextauth]/route";

afterEach(() => vi.unstubAllEnvs());

describe("auth route logout cleanup", () => {
  it("withholds cookie clearing when the logout event fails", async () => {
    vi.stubEnv("NEXTAUTH_URL", "http://localhost:3000");
    const request = new Request("http://localhost:3000/api/auth/signout?fail=1", { method: "POST", headers: { host: "localhost:3000", origin: "http://localhost:3000" } });
    const result = await POST(request, { params: Promise.resolve({ nextauth: ["signout"] }) });
    expect(result.status).toBe(503);
    expect(result.headers.get("set-cookie")).toBeNull();
  });
});
