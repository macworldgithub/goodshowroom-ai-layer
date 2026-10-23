export const TEST_ORIGIN = "http://127.0.0.1:4178";

const allowedPaths = new Set([
  "/test-harness",
  "/api/test-identity",
  "/api/test-data",
  "/test-redirect",
]);

export function isAllowedHarnessRequest(rawUrl, method = "GET") {
  if (method !== "GET") return false;

  try {
    const url = new URL(rawUrl, TEST_ORIGIN);
    return (
      url.protocol === "http:" &&
      url.hostname === "127.0.0.1" &&
      url.port === "4178" &&
      url.username === "" &&
      url.password === "" &&
      url.search === "" &&
      url.hash === "" &&
      allowedPaths.has(url.pathname)
    );
  } catch {
    return false;
  }
}

export function directRequestDenied() {
  return Promise.reject(new Error("Direct API requests are disabled in offline tests."));
}
