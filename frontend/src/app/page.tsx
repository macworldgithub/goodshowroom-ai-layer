import { getServerSession } from "next-auth";
import { configuredAuthOptions } from "../lib/server/auth";

export const dynamic = "force-dynamic";

export default async function Home() {
  let signedIn = false;
  try {
    signedIn = Boolean(await getServerSession(configuredAuthOptions()));
  } catch {
    // The template can still load when local login is unavailable.
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl items-center px-6 py-12">
      <section className="w-full rounded-xl border border-slate-200 bg-white p-8">
        <p className="mb-3 text-sm font-semibold text-blue-700">Local workspace</p>
        <h1 className="text-3xl font-semibold tracking-tight">Good Showroom AI</h1>
        <p className="mt-4 leading-7 text-slate-600">
          Chat will be added in a later task.
        </p>
        <a
          className="mt-6 inline-flex rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-900 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700"
          href={signedIn ? "/api/auth/signout" : "/api/auth/signin"}
        >
          {signedIn ? "Sign out" : "Sign in"}
        </a>
      </section>
    </main>
  );
}
