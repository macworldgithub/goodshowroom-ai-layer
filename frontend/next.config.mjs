import { fileURLToPath } from "node:url";
import { loadRepositoryEnv } from "./scripts/load-repository-env.mjs";

// Existing process values win. Local values override the shared defaults.
// Never export server values through Next.js's client-visible `env` option.
loadRepositoryEnv(fileURLToPath(new URL("../", import.meta.url)));

const development = process.env.NODE_ENV !== "production";
const secureSite = (process.env.NEXTAUTH_URL ?? "").startsWith("https://");

// Next.js writes small inline scripts for hydration, so `script-src` keeps
// 'unsafe-inline'. A per-request nonce is a later hardening step. The page
// loads nothing from another origin and cannot be framed.
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${development ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self'${development ? " ws://127.0.0.1:* ws://localhost:*" : ""}`,
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const pageHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  ...(secureSite ? [{ key: "Strict-Transport-Security", value: "max-age=31536000" }] : []),
];

/** @type {import("next").NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  experimental: { cpus: 1 },
  // Request paths can carry chat and session identifiers. Do not log API requests or full fetch URLs.
  logging: { incomingRequests: { ignore: [/^\/api\//] }, fetches: { fullUrl: false }, browserToTerminal: false },
  async headers() {
    return [
      { source: "/:path*", headers: pageHeaders },
      // Protected and session responses must never be stored by a browser or proxy.
      { source: "/api/:path*", headers: [{ key: "Cache-Control", value: "no-store" }] },
    ];
  },
};

export default nextConfig;
