const safeNames = new Map([
  ["serves only a synthetic identity and API", "synthetic-api"],
  ["blocks external page requests", "external-request-deny"],
  ["blocks redirected local requests", "redirect-deny"],
  ["denies direct API request contexts", "api-context-deny"],
  ["blocks WebSocket requests", "websocket-deny"],
  ["keeps browser contexts isolated", "context-isolation"],
]);

class SafeReporter {
  constructor() {
    this.passed = 0;
    this.failed = 0;
    this.skipped = 0;
    this.discovered = 0;
  }

  onBegin(_config, suite) {
    this.discovered = suite.allTests().length;
  }

  onStdOut() {}
  onStdErr() {}
  onError() {}

  onTestEnd(test, result) {
    const status = result.status;
    if (status === "passed") this.passed += 1;
    else if (status === "skipped" || status === "interrupted") this.skipped += 1;
    else this.failed += 1;
    const name = safeNames.get(test.title) ?? "unrecognized-test";
    const safeStatus = status === "passed" ? "passed" : status === "skipped" ? "skipped" : "failed";
    process.stdout.write(`E2E ${name} ${safeStatus}\n`);
  }

  onEnd(result) {
    const status = result.status === "passed" && this.failed === 0 ? "passed" : "failed";
    process.stdout.write(
      `E2E summary ${status} discovered=${this.discovered} passed=${this.passed} failed=${this.failed} skipped=${this.skipped}\n`,
    );
  }
}

module.exports = SafeReporter;
