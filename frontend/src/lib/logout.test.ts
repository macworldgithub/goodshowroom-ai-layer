import { afterEach, describe, expect, it, vi } from "vitest";
import { coordinateLogout } from "./logout";

describe("coordinateLogout", () => {
  afterEach(() => vi.useRealTimers());

  it("clears local state, invalidates the backend, then signs out", async () => {
    const calls: string[] = [];
    const result = await coordinateLogout({
      clearLocalState: () => calls.push("clear"),
      revokeBackend: async () => { calls.push("revoke"); },
      signOut: async () => { calls.push("sign-out"); },
    });

    expect(calls).toEqual(["clear", "revoke", "sign-out"]);
    expect(result).toEqual({
      localCleared: true,
      backendInvalidated: true,
      signedOut: true,
      cleanupConfirmed: true,
      notice: "Signed out. Temporary chat was cleared.",
    });
  });

  it("reports incomplete cleanup without exposing callback errors", async () => {
    const calls: string[] = [];
    const result = await coordinateLogout({
      clearLocalState: () => { calls.push("clear"); throw new Error("private local detail"); },
      revokeBackend: async () => { calls.push("revoke"); throw new Error("private backend detail"); },
      signOut: async () => { calls.push("sign-out"); throw new Error("private auth detail"); },
    });

    expect(calls).toEqual(["clear", "revoke", "sign-out"]);
    expect(result.localCleared).toBe(false);
    expect(result.backendInvalidated).toBe(false);
    expect(result.signedOut).toBe(false);
    expect(result.cleanupConfirmed).toBe(false);
    expect(result.notice).toContain("login lease expires");
    expect(JSON.stringify(result)).not.toMatch(/private .* detail/);
  });

  it("keeps backend cleanup status when sign-out fails", async () => {
    const result = await coordinateLogout({
      clearLocalState: () => undefined,
      revokeBackend: async () => undefined,
      signOut: async () => { throw new Error("private auth detail"); },
    });

    expect(result).toMatchObject({
      localCleared: true,
      backendInvalidated: true,
      signedOut: false,
      cleanupConfirmed: true,
    });
    expect(result.notice).toContain("Try signing out again");
  });

  it("reports local clearing failure while preserving successful server cleanup", async () => {
    const result = await coordinateLogout({
      clearLocalState: () => { throw new Error("private local detail"); },
      revokeBackend: async () => undefined,
      signOut: async () => undefined,
    });

    expect(result).toMatchObject({
      localCleared: false,
      backendInvalidated: true,
      signedOut: true,
      cleanupConfirmed: false,
    });
    expect(result.notice).toContain("Local temporary chat could not be cleared");
    expect(result.notice).not.toContain("Server cleanup could not be confirmed");
  });

  it("aborts a hung backend call and still attempts sign-out", async () => {
    vi.useFakeTimers();
    let aborted = false;
    const calls: string[] = [];
    const resultPromise = coordinateLogout({
      clearLocalState: () => undefined,
      timeoutMs: 5,
      revokeBackend: (signal) => new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => { aborted = true; reject(new Error("private timeout detail")); });
      }),
      signOut: async () => { calls.push("sign-out"); },
    });
    await vi.advanceTimersByTimeAsync(5);
    const result = await resultPromise;

    expect(aborted).toBe(true);
    expect(calls).toEqual(["sign-out"]);
    expect(result.backendInvalidated).toBe(false);
    expect(result.signedOut).toBe(true);
    expect(JSON.stringify(result)).not.toContain("private timeout detail");
  });
});
