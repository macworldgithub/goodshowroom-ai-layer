import { describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
import { validAuthRequest } from "./auth-request";

describe("auth request origin", () => {
  const expected = new URL("http://localhost:3000");
  function request(headers: Record<string, string>, method = "POST") {
    return new Request("http://localhost:3000/api/auth/signout", { method, headers });
  }
  it("accepts only matching host, forwarded values and POST origin", () => {
    const headers = { host: "localhost:3000", origin: expected.origin, "x-forwarded-host": "localhost:3000", "x-forwarded-proto": "http" };
    expect(validAuthRequest(request(headers), expected)).toBe(true);
    expect(validAuthRequest(request({ ...headers, host: "other.test" }), expected)).toBe(false);
    expect(validAuthRequest(request({ ...headers, "x-forwarded-host": "other.test" }), expected)).toBe(false);
    expect(validAuthRequest(request({ ...headers, "x-forwarded-proto": "https" }), expected)).toBe(false);
    expect(validAuthRequest(request({ ...headers, origin: "http://other.test" }), expected)).toBe(false);
    expect(validAuthRequest(request({ host: "localhost:3000" }, "GET"), expected)).toBe(true);
  });
});
