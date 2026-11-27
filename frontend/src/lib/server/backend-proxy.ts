import "server-only";
import { randomUUID } from "node:crypto";
import { CSRF_HEADER, csrfToken, validCsrf } from "./csrf";
import { validBrowserRequest } from "./browser-request";
import { signAssertion, type SigningConfiguration } from "./identity";
import { validBinding, type Binding } from "./login-transport";
import { matchRoute, queryAllowed, type ProxyRoute } from "./proxy-routes";

/** Backend traffic stays on the loopback interface. */
export const DEFAULT_BACKEND_ORIGIN = "http://127.0.0.1:8000";
const MAX_BODY_BYTES = 60_000; // query + body must stay under the 64 KiB assertion limit
const MAX_RESPONSE_BYTES = 1_048_576;
const RENEW_AFTER_SECONDS = 240; // the backend lease is 300 seconds
const MAX_TRACKED_LOGINS = 1000;
const PASSED_STATUSES = new Set([200, 201, 204, 401, 403, 404, 405, 409, 410, 413, 422, 429, 500, 502, 503, 504]);

/** Fixed messages that match the backend error catalog and the generated validators. */
const ERRORS = {
  authentication_required: [401, "Please sign in.", false],
  access_denied: [403, "You do not have access to this information.", false],
  not_found: [404, "The requested resource is unavailable.", false],
  method_not_allowed: [405, "This request method is not allowed.", false],
  payload_too_large: [413, "The request is too large.", false],
  invalid_request: [422, "The request is invalid.", false],
  source_unavailable: [503, "The data source is unavailable.", false],
  authorization_unavailable: [503, "Access could not be verified.", false],
  deadline_exceeded: [504, "The request timed out. Check its status before retrying.", false],
} as const;
type ErrorCode = keyof typeof ERRORS;

export type ProxyDependencies = {
  /** The parsed `NEXTAUTH_URL`: the only site allowed to call this proxy. */
  expected: URL;
  /** Server secret that derives the CSRF key. Never sent to the browser. */
  secret: string;
  signing: SigningConfiguration;
  /** Returns the verified server-session binding, or null. Never reads browser claims. */
  readBinding: (request: Request) => Promise<unknown>;
  renew: (binding: Binding, now: number) => Promise<void>;
  backendOrigin?: string;
  fetcher?: typeof fetch;
  now?: () => number;
};

/** Last renewal time per login. Module scope, because the proxy is built for each request. */
const renewed = new Map<string, number>();

const SAFE_HEADERS = { "cache-control": "no-store", "content-type": "application/json", "x-content-type-options": "nosniff" };

function problem(code: ErrorCode, commitUnknown = false): Response {
  const [status, message, retryable] = ERRORS[code];
  const error = { code, message, retryable, ...(commitUnknown ? { commit_state: "unknown" } : {}) };
  return new Response(JSON.stringify({ schema_version: 1, request_id: randomUUID(), error }), { status, headers: SAFE_HEADERS });
}

async function readBounded(stream: ReadableStream<Uint8Array> | null, limit: number): Promise<Uint8Array | null> {
  if (!stream) return new Uint8Array();
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) { await reader.cancel(); return null; }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  return new Uint8Array(Buffer.concat(chunks, size));
}

function jsonContentType(value: string | null): boolean {
  return /^application\/json\s*(;\s*charset\s*=\s*"?utf-8"?\s*)?$/i.test(value ?? "");
}

export function createBackendProxy(dependencies: ProxyDependencies) {
  const origin = dependencies.backendOrigin ?? DEFAULT_BACKEND_ORIGIN;
  const parsed = new URL(origin);
  if (parsed.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname) || parsed.pathname !== "/" || parsed.search || parsed.username) {
    throw new Error("Proxy configuration unavailable.");
  }
  const fetcher = dependencies.fetcher ?? fetch;
  const now = dependencies.now ?? (() => Math.floor(Date.now() / 1000));

  async function ensureLease(binding: Binding, time: number): Promise<boolean> {
    const last = renewed.get(binding.login_session_id);
    if (last !== undefined && time - last < RENEW_AFTER_SECONDS) return true;
    try { await dependencies.renew(binding, time); } catch { return false; }
    if (renewed.size >= MAX_TRACKED_LOGINS) renewed.delete(renewed.keys().next().value as string);
    renewed.set(binding.login_session_id, time);
    return true;
  }

  async function forward(request: Request, route: ProxyRoute, backendPath: string, search: string, binding: Binding, time: number): Promise<Response> {
    const mutation = route.method !== "GET";
    let body: Uint8Array = new Uint8Array();
    if (route.body === "json") {
      if (!jsonContentType(request.headers.get("content-type"))) return problem("invalid_request");
      const declared = Number(request.headers.get("content-length") ?? "0");
      if (declared > MAX_BODY_BYTES) return problem("payload_too_large");
      const read = await readBounded(request.body, MAX_BODY_BYTES);
      if (read === null) return problem("payload_too_large");
      if (read.length === 0) return problem("invalid_request");
      body = read;
    } else {
      // A route without a body must receive none: no type, no length, no bytes.
      if (request.headers.get("content-type") || Number(request.headers.get("content-length") ?? "0") > 0) return problem("invalid_request");
      if ((await readBounded(request.body, 0)) === null) return problem("invalid_request");
    }
    if (!(await ensureLease(binding, time))) return problem("authorization_unavailable");
    let assertion: string;
    try {
      assertion = await signAssertion(
        dependencies.signing,
        { purpose: "request", sub: binding.user_id, generation: binding.generation, login_session_id: binding.login_session_id },
        { method: route.method, path: backendPath, query: new TextEncoder().encode(search.slice(1)), body },
        time,
      );
    } catch { return problem("authorization_unavailable"); }
    // Only these headers reach FastAPI. Browser cookies, identity and role headers are dropped.
    const headers: Record<string, string> = { authorization: `Bearer ${assertion}`, accept: "application/json" };
    if (route.body === "json") headers["content-type"] = "application/json";
    let upstream: Response;
    try {
      upstream = await fetcher(`${origin}${backendPath}${search}`, {
        method: route.method, headers, body: body.length ? (body as BodyInit) : undefined,
        redirect: "error", cache: "no-store", signal: AbortSignal.timeout(route.timeoutMs),
      });
    } catch (error) {
      if (mutation || (error instanceof Error && error.name === "TimeoutError")) return problem("deadline_exceeded", mutation);
      return problem("source_unavailable");
    }
    if (!PASSED_STATUSES.has(upstream.status)) { await upstream.body?.cancel(); return problem("source_unavailable"); }
    if (upstream.status === 204) return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
    const declared = Number(upstream.headers.get("content-length") ?? "0");
    const text = declared > MAX_RESPONSE_BYTES ? null : await readBounded(upstream.body, MAX_RESPONSE_BYTES);
    if (text === null || !jsonContentType(upstream.headers.get("content-type"))) return problem("source_unavailable");
    try {
      const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(text));
      if (!value || typeof value !== "object" || value.schema_version !== 1) throw new Error();
    } catch { return problem("source_unavailable"); }
    const headersOut = new Headers(SAFE_HEADERS);
    const retry = upstream.headers.get("retry-after");
    if (upstream.status === 429 && retry && /^\d{1,5}$/.test(retry)) headersOut.set("retry-after", retry);
    return new Response(text as BodyInit, { status: upstream.status, headers: headersOut });
  }

  return async function handle(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (!validBrowserRequest(request, dependencies.expected)) return problem("access_denied");
    const csrfRequest = request.method === "GET" && url.pathname === "/api/v1/csrf";
    const match = csrfRequest ? null : matchRoute(request.method, url.pathname);
    if (match?.kind === "not_found") return problem("not_found");
    if (match?.kind === "method_not_allowed") return problem("method_not_allowed");
    let binding: unknown;
    try { binding = await dependencies.readBinding(request); } catch { binding = null; }
    const time = now();
    if (!validBinding(binding, time)) return problem("authentication_required");
    if (csrfRequest) {
      if (url.search) return problem("invalid_request");
      const data = { csrf_token: csrfToken(dependencies.secret, binding.login_session_id) };
      return new Response(JSON.stringify({ schema_version: 1, request_id: randomUUID(), data }), { status: 200, headers: SAFE_HEADERS });
    }
    if (match?.kind !== "route") return problem("not_found");
    if (match.route.method !== "GET" && !validCsrf(dependencies.secret, binding.login_session_id, request.headers.get(CSRF_HEADER))) {
      return problem("access_denied");
    }
    if (!queryAllowed(match.route, url.search)) return problem("invalid_request");
    return forward(request, match.route, match.backendPath, url.search, binding, time);
  };
}
