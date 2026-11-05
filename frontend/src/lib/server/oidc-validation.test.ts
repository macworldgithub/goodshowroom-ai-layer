// @vitest-environment node
import { createRequire } from "node:module";
import { SignJWT } from "jose";
import { describe, expect, it } from "vitest";

const openid = createRequire(import.meta.url)("openid-client");
const secret = "synthetic-only-signing-secret-32-bytes-minimum";

describe("OpenID claim verification", () => {
  it("rejects wrong issuer, audience, expiry, and signature with synthetic tokens", async () => {
    const issuer = new openid.Issuer({ issuer: "https://accounts.google.com" });
    const client = new issuer.Client({ client_id: "synthetic-client", client_secret: secret, id_token_signed_response_alg: "HS256" });
    const now = Math.floor(Date.now() / 1000);
    async function token(overrides: Record<string, unknown> = {}, key = secret) {
      return new SignJWT({ iss: "https://accounts.google.com", aud: "synthetic-client", sub: "synthetic", iat: now, exp: now + 300, ...overrides })
        .setProtectedHeader({ alg: "HS256" }).sign(new TextEncoder().encode(key));
    }
    await expect(client.validateIdToken(await token(), undefined, "token")).resolves.toBeDefined();
    for (const invalid of [
      await token({ iss: "https://attacker.invalid" }),
      await token({ aud: "other-client" }),
      await token({ exp: now - 60 }),
      await token({}, "wrong-synthetic-signing-secret-32-bytes-minimum"),
    ]) {
      await expect(client.validateIdToken(invalid, undefined, "token")).rejects.toThrow();
    }
  });
});
