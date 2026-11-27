import "server-only";

const MUTATIONS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Check that a browser request came from the configured site. `expected` is
 * the parsed `NEXTAUTH_URL`. The Host header and any forwarding headers must
 * match it exactly. A mutation must carry an exact `Origin`. A cross-site
 * `Sec-Fetch-Site` value is always refused. No other header is trusted.
 */
export function validBrowserRequest(request: Request, expected: URL): boolean {
  const host = request.headers.get("host");
  const forwardedHost = request.headers.get("x-forwarded-host");
  const forwardedProto = request.headers.get("x-forwarded-proto");
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  if (host?.toLowerCase() !== expected.host.toLowerCase()) return false;
  if (forwardedHost && forwardedHost.toLowerCase() !== expected.host.toLowerCase()) return false;
  if (forwardedProto && forwardedProto !== expected.protocol.slice(0, -1)) return false;
  if (site && site !== "same-origin") return false;
  if (MUTATIONS.has(request.method)) return origin === expected.origin;
  return origin === null || origin === expected.origin;
}
