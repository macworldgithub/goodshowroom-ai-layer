import { fileURLToPath } from "node:url";
import { loadRepositoryEnv } from "./scripts/load-repository-env.mjs";

// Existing process values win. Local values override the shared defaults.
// Never export server values through Next.js's client-visible `env` option.
loadRepositoryEnv(fileURLToPath(new URL("../", import.meta.url)));

/** @type {import("next").NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  experimental: { cpus: 1 },
};

export default nextConfig;
