import "server-only";
import type { NextApiRequest } from "next";
import { getToken } from "next-auth/jwt";
import { signingConfigurationFromEnv } from "./auth";
import { createBackendProxy, DEFAULT_BACKEND_ORIGIN } from "./backend-proxy";
import { createLoginTransport } from "./login-transport";

/**
 * Read only the cookies. `getToken` would also accept an `Authorization: Bearer`
 * header as a session, so the headers are withheld on purpose.
 */
function cookiesOnly(request: Request): NextApiRequest {
  const cookies: Record<string, string> = {};
  for (const part of (request.headers.get("cookie") ?? "").split(";")) {
    const at = part.indexOf("=");
    if (at < 1) continue;
    const name = part.slice(0, at).trim();
    let value = part.slice(at + 1).trim();
    try { value = decodeURIComponent(value); } catch { continue; }
    if (!(name in cookies)) cookies[name] = value;
  }
  return { cookies, headers: {} } as unknown as NextApiRequest;
}

/**
 * Build the proxy from server environment values. This runs on each request so
 * that a missing or invalid setting fails closed with a safe error. The browser
 * never supplies the site, the backend target, or the signing material.
 */
export function configuredBackendProxy() {
  const secret = process.env.AUTH_SECRET ?? "";
  if (Buffer.byteLength(secret, "utf8") < 32) throw new Error("Proxy configuration unavailable.");
  const expected = new URL(process.env.NEXTAUTH_URL ?? "");
  const secure = expected.protocol === "https:";
  const signing = signingConfigurationFromEnv();
  const transport = createLoginTransport(signing, DEFAULT_BACKEND_ORIGIN);
  return createBackendProxy({
    expected: new URL(expected.origin),
    secret,
    signing,
    backendOrigin: DEFAULT_BACKEND_ORIGIN,
    // The session cookie is decoded and verified by NextAuth. Only its server-set binding is used.
    readBinding: async (request) => (await getToken({ req: cookiesOnly(request), secret, secureCookie: secure }))?.binding ?? null,
    renew: (binding, now) => transport.change(binding, "renew", now),
  });
}
