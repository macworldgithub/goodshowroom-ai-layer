import { describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
import { authLogger, authRequestContext } from "./auth-logger";

describe("logout failure isolation", () => {
  it("keeps concurrent request failure state separate", async () => {
    const failed = { logoutFailed: false };
    const successful = { logoutFailed: false };
    await Promise.all([
      authRequestContext.run(failed, async () => { await Promise.resolve(); authLogger.error("SIGN_OUT_EVENT_ERROR"); }),
      authRequestContext.run(successful, async () => { await Promise.resolve(); authLogger.error("UNRELATED_ERROR"); }),
    ]);
    expect(failed.logoutFailed).toBe(true);
    expect(successful.logoutFailed).toBe(false);
  });
});
