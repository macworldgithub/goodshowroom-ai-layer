import "server-only";
import { createHmac, hkdfSync, timingSafeEqual } from "node:crypto";

/**
 * Session-bound CSRF token. The token is an HMAC of the login session ID under
 * a key derived from the server secret. It is not stored. A token from one
 * login never works for another, and a forged token cannot be computed without
 * the server secret. The browser sends it in `x-csrf-token` on every mutation.
 */
export const CSRF_HEADER = "x-csrf-token";

function key(secret: string): Buffer {
  return Buffer.from(hkdfSync("sha256", Buffer.from(secret, "utf8"), Buffer.alloc(0), "goodshowroom-csrf-v1", 32));
}

export function csrfToken(secret: string, loginSessionId: string): string {
  return createHmac("sha256", key(secret)).update(loginSessionId, "utf8").digest("base64url");
}

export function validCsrf(secret: string, loginSessionId: string, presented: string | null): boolean {
  if (!presented || presented.length !== 43 || !/^[A-Za-z0-9_-]+$/.test(presented)) return false;
  const expected = Buffer.from(csrfToken(secret, loginSessionId));
  const actual = Buffer.from(presented);
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}
