/** Coordinate the existing NextAuth sign-out and protected backend revoke callbacks.
 * The UI supplies in-memory state clearing; this helper stores no chat data.
 * Routes, CSRF/identity transport and screens remain with their registered tickets.
 */
export type LogoutActions = {
  revokeBackend: (signal: AbortSignal) => Promise<void>;
  signOut: () => Promise<void>;
  clearLocalState: () => void;
  timeoutMs?: number;
};

async function attempt(action: (signal: AbortSignal) => Promise<void>, timeoutMs: number) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      Promise.resolve().then(() => action(controller.signal)),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("Logout step expired."));
        }, timeoutMs);
      }),
    ]);
    return true;
  } catch {
    return false;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export async function coordinateLogout(actions: LogoutActions) {
  let localCleared = false;
  try {
    actions.clearLocalState();
    localCleared = true;
  } catch { /* Continue both invalidation attempts; return safe flags only. */ }
  const requestedTimeout = actions.timeoutMs;
  const timeoutMs = typeof requestedTimeout === "number" && Number.isInteger(requestedTimeout) && requestedTimeout > 0 && requestedTimeout <= 60_000
    ? requestedTimeout : 10_000;
  const backendInvalidated = await attempt(actions.revokeBackend, timeoutMs);
  const signedOut = await attempt(() => actions.signOut(), timeoutMs);
  const cleanupConfirmed = localCleared && backendInvalidated;
  const notices: string[] = [];
  if (!localCleared) notices.push("Local temporary chat could not be cleared. Close this page.");
  if (!backendInvalidated) notices.push("Server cleanup could not be confirmed. Temporary context can remain until the login lease expires and cleanup runs.");
  if (!signedOut) notices.push("Sign-out could not be confirmed. Try signing out again.");
  const notice = notices.length ? notices.join(" ") : "Signed out. Temporary chat was cleared.";
  return { localCleared, backendInvalidated, signedOut, cleanupConfirmed, notice };
}
