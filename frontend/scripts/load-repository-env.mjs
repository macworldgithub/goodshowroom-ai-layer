import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { join } from "node:path";

// loadEnvFile preserves existing process values, so the local file goes first.
export function loadRepositoryEnv(directory) {
  for (const name of [".env.local", ".env"]) {
    const path = join(directory, name);
    if (!existsSync(path)) continue;
    try {
      loadEnvFile(path);
    } catch {
      throw new Error("Cannot load server environment configuration.");
    }
  }
}
