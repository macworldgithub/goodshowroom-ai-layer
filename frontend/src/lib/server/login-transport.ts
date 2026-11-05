import "server-only";
import { signAssertion, type SigningConfiguration } from "./identity";

type Bootstrap = { purpose: "bootstrap"; provider: "google"; provider_account_id: string; email: string; email_verified: true; auth_started_at: number; auth_expires_at: number; login_session_id: string };
export type Binding = { user_id: string; login_session_id: string; generation: number; auth_started_at: number; auth_expires_at: number };
type RequestIdentity = { purpose: "request"; sub: string; login_session_id: string; generation: number };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function validBinding(value: unknown, now: number): value is Binding {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const binding = value as Record<string, unknown>;
  return Object.keys(binding).sort().join() === ["auth_expires_at", "auth_started_at", "generation", "login_session_id", "user_id"].join() &&
    typeof binding.user_id === "string" && uuid.test(binding.user_id) && typeof binding.login_session_id === "string" && uuid.test(binding.login_session_id) &&
    Number.isSafeInteger(binding.generation) && (binding.generation as number) >= 0 && Number.isSafeInteger(binding.auth_started_at) && Number.isSafeInteger(binding.auth_expires_at) &&
    (binding.auth_started_at as number) <= now && now < Math.min(binding.auth_expires_at as number, (binding.auth_started_at as number) + 28800);
}

export function createLoginTransport(config: SigningConfiguration) {
  async function send(path: string, identity: Bootstrap | RequestIdentity, now: number): Promise<unknown> {
    const assertion = await signAssertion(config, identity, { method: "POST", path }, now);
    let response: Response;
    try { response = await fetch(`http://127.0.0.1:8000${path}`, { method: "POST", headers: { authorization: `Bearer ${assertion}` }, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000) }); }
    catch { throw new Error("Login service unavailable."); }
    const expectedStatus = path === "/v1/internal/login/revoke" ? 204 : 200;
    if (response.status !== expectedStatus) throw new Error("Login service unavailable.");
    if (response.status === 204) return null;
    const length = Number(response.headers.get("content-length"));
    if (Number.isFinite(length) && length > 8192) throw new Error("Login service unavailable.");
    if (!response.body) throw new Error("Login service unavailable.");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 8192) { await reader.cancel(); throw new Error("Login service unavailable."); }
        chunks.push(value);
      }
    } catch { throw new Error("Login service unavailable."); }
    finally { reader.releaseLock(); }
    const body = Buffer.concat(chunks, size).toString("utf8");
    try { return JSON.parse(body); } catch { throw new Error("Login service unavailable."); }
  }
  return {
    async exchange(identity: Bootstrap, now: number): Promise<Binding> {
      const result = await send("/v1/internal/login/exchange", identity, now) as { schema_version?: unknown; data?: Record<string, unknown> };
      const data = result?.data;
      const binding = { user_id: data?.user_id, login_session_id: data?.login_session_id, generation: data?.generation, auth_started_at: identity.auth_started_at, auth_expires_at: identity.auth_expires_at };
      if (result?.schema_version !== 1 || !validBinding(binding, now) || binding.login_session_id !== identity.login_session_id || !["pending", "approved", "rejected", "revoked"].includes(String(data?.access_status))) throw new Error("Login service unavailable.");
      return binding;
    },
    async change(binding: Binding, operation: "renew" | "revoke", now: number): Promise<void> {
      if (!validBinding(binding, now)) throw new Error("Login service unavailable.");
      const result = await send(`/v1/internal/login/${operation}`, { purpose: "request", sub: binding.user_id, login_session_id: binding.login_session_id, generation: binding.generation }, now);
      if (operation === "renew") {
        const envelope = result as { schema_version?: unknown; data?: { lease_valid_until?: unknown } } | null;
        const value = envelope?.data?.lease_valid_until;
        const lease = typeof value === "string" ? Date.parse(value) / 1000 : NaN;
        if (envelope?.schema_version !== 1 || !Number.isFinite(lease) || lease <= now || lease > Math.min(binding.auth_expires_at, binding.auth_started_at + 28800, now + 330)) throw new Error("Login service unavailable.");
      }
    },
  };
}
