import "server-only";
import { AsyncLocalStorage } from "node:async_hooks";

// NextAuth V4 uses a process-wide logger. Keep request state in async context.
export const authRequestContext = new AsyncLocalStorage<{ logoutFailed: boolean }>();
export const authLogger = {
  error(code: string) {
    const context = authRequestContext.getStore();
    if (context && (code === "SIGN_OUT_EVENT_ERROR" || code === "SIGNOUT_ERROR")) context.logoutFailed = true;
  },
  warn() {},
  debug() {},
};
