import NextAuth from "next-auth";
import { configuredAuthOptions } from "../../../../lib/server/auth";
import { authRequestContext } from "../../../../lib/server/auth-logger";
import { validAuthRequest } from "../../../../lib/server/auth-request";

export const runtime = "nodejs";

async function handler(request: Request, context: { params: Promise<{ nextauth: string[] }> }) {
  try {
    const options = configuredAuthOptions();
    const expected = new URL(process.env.NEXTAUTH_URL!);
    if (!validAuthRequest(request, expected)) {
      return Response.json({ error: "Login request denied." }, { status: 403, headers: { "Cache-Control": "no-store" } });
    }
    const state = { logoutFailed: false };
    return await authRequestContext.run(state, async () => {
      const response = await NextAuth(options)(request, context);
      if (request.method === "POST" && state.logoutFailed) {
        return Response.json({ error: "Logout cleanup could not be confirmed." }, { status: 503, headers: { "Cache-Control": "no-store" } });
      }
      return response;
    });
  } catch {
    return Response.json({ error: "Login service unavailable." }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export { handler as GET, handler as POST };
