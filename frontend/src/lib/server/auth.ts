import "server-only";
import { randomUUID } from "node:crypto";
import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import { googleIdentityFromVerifiedCallback } from "./google-identity";
import { createLoginTransport, validBinding, type Binding } from "./login-transport";
import { authLogger } from "./auth-logger";

const lifetime = 8 * 60 * 60;

export function makeAuthOptions(settings: { clientId: string; clientSecret: string; secret: string; transport: ReturnType<typeof createLoginTransport>; now?: () => number; forceConsent?: boolean }): NextAuthOptions {
  if (!settings.clientId || !settings.clientSecret || Buffer.byteLength(settings.secret, "utf8") < 32) throw new Error("Login configuration unavailable.");
  const now = settings.now ?? (() => Math.floor(Date.now() / 1000));
  return {
    secret: settings.secret,
    providers: [GoogleProvider({ clientId: settings.clientId, clientSecret: settings.clientSecret, ...(settings.forceConsent ? { authorization: { params: { prompt: "consent" } } } : {}) })],
    session: { strategy: "jwt", maxAge: lifetime },
    jwt: { maxAge: lifetime },
    debug: false,
    logger: authLogger,
    callbacks: {
      async signIn({ account, profile }) {
        try { googleIdentityFromVerifiedCallback(account?.provider, account?.providerAccountId, profile); return true; }
        catch { return false; }
      },
      async jwt({ token, account, profile }) {
        const time = now();
        if (account) {
          const identity = googleIdentityFromVerifiedCallback(account.provider, account.providerAccountId, profile);
          const binding = await settings.transport.exchange({ ...identity, purpose: "bootstrap", auth_started_at: time, auth_expires_at: time + lifetime, login_session_id: randomUUID() }, time);
          return { name: token.name, email: token.email, picture: token.picture, binding };
        }
        if (!validBinding(token.binding, time)) throw new Error("Login session unavailable.");
        await settings.transport.change(token.binding, "renew", time);
        return token;
      },
      async session({ session, token }) {
        if (!validBinding(token.binding, now())) throw new Error("Login session unavailable.");
        return { user: session.user, expires: new Date((token.binding as Binding).auth_expires_at * 1000).toISOString() };
      },
    },
    events: {
      async signOut(message) {
        if (!("token" in message) || !validBinding(message.token?.binding, now())) throw new Error("Logout cleanup could not be confirmed.");
        await settings.transport.change(message.token.binding, "revoke", now());
      },
    },
  };
}

export function configuredAuthOptions(): NextAuthOptions {
  const rawUrl = process.env.NEXTAUTH_URL ?? "";
  let url: URL;
  try { url = new URL(rawUrl); } catch { throw new Error("Login configuration unavailable."); }
  if (url.username || url.password || url.search || url.hash || (url.pathname !== "/" && url.pathname !== "/api/auth") || (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) throw new Error("Login configuration unavailable.");
  const consentSetting = process.env.GOOGLE_TEST_FORCE_CONSENT;
  if (consentSetting !== undefined && consentSetting !== "true" && consentSetting !== "false") throw new Error("Login configuration unavailable.");
  const forceConsent = consentSetting === "true";
  if (forceConsent && (process.env.APP_ENV !== "test" || url.origin !== "http://localhost:3000")) throw new Error("Login configuration unavailable.");
  const activatedAt = Number(process.env.IDENTITY_KEY_ACTIVATED_AT);
  if (!Number.isSafeInteger(activatedAt) || activatedAt <= 0) throw new Error("Login configuration unavailable.");
  const transport = createLoginTransport({ issuer: process.env.IDENTITY_ISSUER ?? "", audience: process.env.IDENTITY_AUDIENCE ?? "", kid: process.env.IDENTITY_KEY_ID ?? "", privatePem: (process.env.IDENTITY_PRIVATE_KEY ?? "").replace(/\\n/g, "\n"), activatedAt, revoked: false });
  return makeAuthOptions({ clientId: process.env.GOOGLE_CLIENT_ID ?? "", clientSecret: process.env.GOOGLE_CLIENT_SECRET ?? "", secret: process.env.AUTH_SECRET ?? "", transport, forceConsent });
}
