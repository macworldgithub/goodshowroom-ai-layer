const safeUnitNames = new Map([
  ["renders the current local workspace page", "home-page"],
]);

export default class SafeVitestReporter {
  constructor() {
    this.passed = 0;
    this.failed = 0;
    this.skipped = 0;
  }

  onUserConsoleLog() {}

  onTestCaseResult(testCase) {
    const result = testCase.result();
    const status = result.state;
    if (status === "passed") this.passed += 1;
    else if (status === "skipped" || status === "pending") this.skipped += 1;
    else this.failed += 1;
    const safeName = safeUnitNames.get(testCase.name) ?? "unrecognized-test";
    process.stdout.write(`UNIT ${safeName} ${status === "passed" ? "passed" : status === "skipped" ? "skipped" : "failed"}\n`);
  }

  onTestRunEnd(_testModules, unhandledErrors, reason) {
    if (unhandledErrors.length > 0 || reason === "failed") this.failed += unhandledErrors.length ? 1 : 0;
    const status = this.failed > 0 || reason === "failed" ? "failed" : "passed";
    process.stdout.write(
      `UNIT summary ${status} passed=${this.passed} failed=${this.failed} skipped=${this.skipped}\n`,
    );
  }
}
