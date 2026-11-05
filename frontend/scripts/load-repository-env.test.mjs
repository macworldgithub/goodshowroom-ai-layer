import assert from "node:assert/strict";
import { mkdtempSync, rmdirSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { loadRepositoryEnv } from "./load-repository-env.mjs";

test("local settings override defaults and preserve process values", () => {
  const directory = mkdtempSync(join(tmpdir(), "gsr-env-test-"));
  const names = ["GSR_TEST_OVERRIDE", "GSR_TEST_KEEP", "GSR_TEST_PROCESS"];
  try {
    writeFileSync(join(directory, ".env"), "GSR_TEST_OVERRIDE=base\nGSR_TEST_KEEP=retained\nGSR_TEST_PROCESS=file\n");
    writeFileSync(join(directory, ".env.local"), "GSR_TEST_OVERRIDE=local\n");
    process.env.GSR_TEST_PROCESS = "process";
    loadRepositoryEnv(directory);
    assert.equal(process.env.GSR_TEST_OVERRIDE, "local");
    assert.equal(process.env.GSR_TEST_KEEP, "retained");
    assert.equal(process.env.GSR_TEST_PROCESS, "process");
  } finally {
    for (const name of names) delete process.env[name];
    unlinkSync(join(directory, ".env.local"));
    unlinkSync(join(directory, ".env"));
    rmdirSync(directory);
  }
});
