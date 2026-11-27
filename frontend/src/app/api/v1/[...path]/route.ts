import { configuredBackendProxy } from "../../../../lib/server/proxy-config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const unavailable = () =>
  Response.json(
    { schema_version: 1, request_id: crypto.randomUUID(), error: { code: "source_unavailable", message: "The data source is unavailable.", retryable: false } },
    { status: 503, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } },
  );

async function handler(request: Request): Promise<Response> {
  try {
    return await configuredBackendProxy()(request);
  } catch {
    return unavailable();
  }
}

// Only these methods exist. Any other method receives the framework's 405.
export { handler as GET, handler as POST, handler as PUT, handler as PATCH, handler as DELETE };
