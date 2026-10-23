import { createTestServer } from "./e2e-test-server.mjs";

const host = "127.0.0.1";
const port = 4178;

export default async function setupSyntheticTestServer() {
  const server = createTestServer();

  try {
    await new Promise((resolve, reject) => {
      const onError = () => reject(new Error("Synthetic test server could not start."));
      server.once("error", onError);
      server.listen(port, host, () => {
        server.off("error", onError);
        resolve();
      });
    });
  } catch {
    throw new Error("Synthetic test server could not start.");
  }

  return async function stopSyntheticTestServer() {
    if (!server.listening) return;

    const closed = new Promise((resolve, reject) => {
      server.close((error) => {
        if (error) reject(new Error("Synthetic test server could not stop."));
        else resolve();
      });
    });
    server.closeAllConnections();
    await closed;
  };
}
