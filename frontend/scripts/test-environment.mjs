import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const allowedVariables = new Set([
  "PATH",
  "SYSTEMROOT",
  "WINDIR",
  "COMSPEC",
  "PATHEXT",
  "TEMP",
  "TMP",
  "USERPROFILE",
  "APPDATA",
  "LOCALAPPDATA",
  "HOME",
  "HOMEDRIVE",
  "HOMEPATH",
  "XDG_CACHE_HOME",
]);

export function createTestEnvironment(extra = {}) {
  const environment = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (allowedVariables.has(name.toUpperCase())) environment[name] = value;
  }
  const preload = pathToFileURL(resolve(frontendRoot, "scripts/privacy-guard.mjs")).href;
  return {
    ...environment,
    ...extra,
    GOODSHOWROOM_PRIVACY_GUARD: "active",
    NODE_OPTIONS: `--import=${preload}`,
  };
}
