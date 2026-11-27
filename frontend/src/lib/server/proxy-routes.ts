import "server-only";

/**
 * The only browser operations that may reach FastAPI. Anything absent from
 * this table is refused. Paths are relative to `/api/v1` (browser) and `/v1`
 * (backend). Add a row here when a later ticket adds a backend route.
 */
export type ProxyMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type ProxyRoute = {
  method: ProxyMethod;
  /** Segments starting with `:` are typed parameters: `:uuid` or `:slug`. */
  template: string;
  /** Query keys the backend accepts. Any other key is refused. */
  query: readonly string[];
  /** `json` requires an `application/json` body. `none` requires an empty body. */
  body: "json" | "none";
  timeoutMs: number;
};

const PAGE = ["page_size", "cursor"] as const;
const SHORT = 15_000;
const TURN = 120_000;

export const PROXY_ROUTES: readonly ProxyRoute[] = [
  { method: "GET", template: "/me", query: [], body: "none", timeoutMs: SHORT },
  { method: "POST", template: "/access-requests", query: [], body: "none", timeoutMs: SHORT },
  { method: "GET", template: "/admin/access-requests", query: ["status", ...PAGE], body: "none", timeoutMs: SHORT },
  { method: "POST", template: "/admin/access-requests/:uuid/decision", query: [], body: "json", timeoutMs: SHORT },
  { method: "POST", template: "/admin/preapprovals", query: [], body: "json", timeoutMs: SHORT },
  { method: "POST", template: "/admin/preapprovals/:uuid/revoke", query: [], body: "json", timeoutMs: SHORT },
  { method: "GET", template: "/admin/users", query: ["approval_status", ...PAGE], body: "none", timeoutMs: SHORT },
  { method: "GET", template: "/admin/roles", query: ["active", ...PAGE], body: "none", timeoutMs: SHORT },
  { method: "POST", template: "/admin/roles", query: [], body: "json", timeoutMs: SHORT },
  { method: "PATCH", template: "/admin/roles/:uuid", query: [], body: "json", timeoutMs: SHORT },
  { method: "PATCH", template: "/admin/users/:uuid/access", query: [], body: "json", timeoutMs: SHORT },
  { method: "GET", template: "/admin/access-options", query: [], body: "none", timeoutMs: SHORT },
  { method: "PUT", template: "/admin/users/:uuid/identity-mappings/:slug", query: [], body: "json", timeoutMs: SHORT },
  { method: "GET", template: "/admin/settings", query: [], body: "none", timeoutMs: SHORT },
  { method: "PATCH", template: "/admin/settings", query: [], body: "json", timeoutMs: SHORT },
  { method: "POST", template: "/saved-chats", query: [], body: "json", timeoutMs: SHORT },
  { method: "GET", template: "/saved-chats", query: [...PAGE], body: "none", timeoutMs: SHORT },
  { method: "POST", template: "/saved-chats/:uuid/turns", query: [], body: "json", timeoutMs: TURN },
  { method: "GET", template: "/saved-chats/:uuid/turns", query: [...PAGE], body: "none", timeoutMs: SHORT },
  { method: "GET", template: "/saved-chats/:uuid/requests/:uuid", query: [], body: "none", timeoutMs: SHORT },
  { method: "DELETE", template: "/saved-chats/:uuid", query: [], body: "none", timeoutMs: SHORT },
  { method: "POST", template: "/temporary-chats", query: [], body: "json", timeoutMs: SHORT },
  { method: "POST", template: "/temporary-chats/:uuid/turns", query: [], body: "json", timeoutMs: TURN },
  { method: "GET", template: "/temporary-chats/:uuid/requests/:uuid", query: [], body: "none", timeoutMs: SHORT },
  { method: "DELETE", template: "/temporary-chats/:uuid", query: [], body: "none", timeoutMs: SHORT },
  { method: "GET", template: "/admin/conversations", query: ["user_id", "since", "until", ...PAGE], body: "none", timeoutMs: SHORT },
  { method: "GET", template: "/admin/conversations/:uuid/turns", query: ["user_id", ...PAGE], body: "none", timeoutMs: SHORT },
];

export const BROWSER_PREFIX = "/api/v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
const SAFE_PATH = /^\/[A-Za-z0-9/_-]*$/;
const MAX_QUERY_BYTES = 4600;

export type RouteMatch =
  | { kind: "route"; route: ProxyRoute; backendPath: string }
  | { kind: "method_not_allowed" }
  | { kind: "not_found" };

/**
 * Match a browser pathname to one allowlisted route. The path must already be
 * the raw `URL.pathname`. Encoded characters, dot segments, empty segments and
 * trailing slashes are all refused, so no path can escape the table.
 */
export function matchRoute(method: string, pathname: string): RouteMatch {
  if (!pathname.startsWith(`${BROWSER_PREFIX}/`) || !SAFE_PATH.test(pathname) || pathname.includes("//") || pathname.endsWith("/")) {
    return { kind: "not_found" };
  }
  const relative = pathname.slice(BROWSER_PREFIX.length);
  const parts = relative.split("/").slice(1);
  let pathKnown = false;
  for (const route of PROXY_ROUTES) {
    const template = route.template.split("/").slice(1);
    if (template.length !== parts.length) continue;
    const fits = template.every((segment, index) => {
      if (segment === ":uuid") return UUID.test(parts[index]);
      if (segment === ":slug") return SLUG.test(parts[index]);
      return segment === parts[index];
    });
    if (!fits) continue;
    pathKnown = true;
    if (route.method === method) return { kind: "route", route, backendPath: `/v1${relative}` };
  }
  return pathKnown ? { kind: "method_not_allowed" } : { kind: "not_found" };
}

/**
 * Check the raw query string against the route's key allowlist. Duplicate or
 * unknown keys, non-ASCII text, and oversized queries are refused. The backend
 * still validates every value; this only keeps the forwarded query small.
 */
export function queryAllowed(route: ProxyRoute, search: string): boolean {
  if (search === "") return true;
  if (!search.startsWith("?") || search.length - 1 > MAX_QUERY_BYTES || !/^[\x21-\x7e]+$/.test(search)) return false;
  const seen = new Set<string>();
  for (const pair of search.slice(1).split("&")) {
    const key = pair.split("=", 1)[0];
    if (!route.query.includes(key) || seen.has(key) || !pair.includes("=")) return false;
    seen.add(key);
  }
  return true;
}
