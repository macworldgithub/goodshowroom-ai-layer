import "server-only";

export function validAuthRequest(request: Request, expected: URL): boolean {
  const host = request.headers.get("host");
  const forwardedHost = request.headers.get("x-forwarded-host");
  const forwardedProto = request.headers.get("x-forwarded-proto");
  return host?.toLowerCase() === expected.host.toLowerCase() &&
    (!forwardedHost || forwardedHost.toLowerCase() === expected.host.toLowerCase()) &&
    (!forwardedProto || forwardedProto === expected.protocol.slice(0, -1)) &&
    (request.method !== "POST" || request.headers.get("origin") === expected.origin);
}
