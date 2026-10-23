import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { fileURLToPath } from "node:url";

// Load only the existing repository file. Never export its values through `env`.
const repositoryEnv = fileURLToPath(new URL("../.env", import.meta.url));
if (existsSync(repositoryEnv)) {
  try {
    loadEnvFile(repositoryEnv);
  } catch {
    throw new Error("Cannot load server environment configuration.");
  }
}

/** @type {import("next").NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  experimental: { cpus: 1 },
};

export default nextConfig;
