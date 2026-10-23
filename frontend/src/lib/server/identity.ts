import "server-only";
import { createHash, createPrivateKey, randomUUID } from "node:crypto";
import { importPKCS8, SignJWT } from "jose";

type Identity =
  | { purpose: "request"; sub: string; generation: number; login_session_id: string }
  | { purpose: "bootstrap"; provider: "google"; provider_account_id: string; email: string; email_verified: true; auth_started_at: number; auth_expires_at: number; login_session_id: string };

export type SigningConfiguration = {
  issuer: string;
  audience: string;
  kid: string;
  privatePem: string;
  activatedAt: number;
  revoked: boolean;
  retiredAt?: number;
};

export function operationDigest(method: string, path: string, query: Uint8Array = new Uint8Array(), body: Uint8Array = new Uint8Array()): string {
  if (!["GET", "POST", "PUT", "PATCH", "DELETE"].includes(method) || path.match(/^\/[A-Za-z0-9/_-]*$/)?.[0] !== path || path.includes("//") || !(query instanceof Uint8Array) || !(body instanceof Uint8Array) || query.length + body.length > 65536) throw new Error("Identity assertion denied.");
  const length = Buffer.alloc(4);
  length.writeUInt32BE(query.length);
  return createHash("sha256").update(length).update(query).update(body).digest("hex");
}

/** Only verified server-session data may reach this module. No browser-supplied claims. */
export async function signAssertion(config: SigningConfiguration, identity: Identity, operation: { method: string; path: string; query?: Uint8Array; body?: Uint8Array }, now: number): Promise<string> {
  try {
    if (!Number.isSafeInteger(now) || !Number.isSafeInteger(config.activatedAt) || !config.issuer || !config.audience || !/^[A-Za-z0-9_-]{1,64}$/.test(config.kid) || config.revoked !== false || config.retiredAt !== undefined || now < config.activatedAt || now >= config.activatedAt + 90 * 86400) throw new Error();
    const fields = identity.purpose === "request" ? ["purpose", "sub", "generation", "login_session_id"] : ["purpose", "provider", "provider_account_id", "email", "email_verified", "auth_started_at", "auth_expires_at", "login_session_id"];
    if (Object.keys(identity).sort().join() !== fields.sort().join() || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(identity.login_session_id)) throw new Error();
    if (identity.purpose === "request") {
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(identity.sub) || !Number.isSafeInteger(identity.generation) || identity.generation < 0) throw new Error();
    } else if (identity.purpose !== "bootstrap" || identity.provider !== "google" || identity.email_verified !== true || typeof identity.provider_account_id !== "string" || identity.provider_account_id.length < 1 || identity.provider_account_id.length > 255 || typeof identity.email !== "string" || identity.email.length > 254 || !/^[^\s@]+@[^\s@]+$/.test(identity.email) || !Number.isSafeInteger(identity.auth_started_at) || !Number.isSafeInteger(identity.auth_expires_at) || identity.auth_started_at > now || now >= Math.min(identity.auth_expires_at, identity.auth_started_at + 28800)) throw new Error();
    const nodeKey = createPrivateKey(config.privatePem);
    if (nodeKey.asymmetricKeyType !== "rsa" || nodeKey.asymmetricKeyDetails?.modulusLength !== 3072) throw new Error();
    const digest = operationDigest(operation.method, operation.path, operation.query, operation.body);
    const key = await importPKCS8(config.privatePem, "RS256");
    const token = await new SignJWT({ ...identity, method: operation.method, path: operation.path, digest })
      .setProtectedHeader({ alg: "RS256", typ: "JWT", kid: config.kid })
      .setIssuer(config.issuer).setAudience(config.audience).setIssuedAt(now).setNotBefore(now).setExpirationTime(now + 120).setJti(randomUUID()).sign(key);
    if (token.length > 8192 || config.revoked || config.retiredAt !== undefined) throw new Error();
    return token;
  } catch {
    throw new Error("Identity assertion denied.");
  }
}
