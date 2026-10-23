import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

if (process.env.GOODSHOWROOM_PRIVACY_GUARD !== "active") {
  throw new Error("Use the fixed unit test launcher.");
}

export default defineConfig({
  plugins: [react()],
  envDir: false,
  test: {
    include: ["src/**/*.test.tsx"],
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    clearMocks: true,
    restoreMocks: true,
    reporters: [["./scripts/safe-vitest-reporter.mjs"]],
  },
});
