// Resolve the existing TypeScript import for the isolated Node HTTP check.
export async function resolve(specifier, context, nextResolve) {
  if (specifier === "./identity" && context.parentURL?.endsWith("/login-transport.ts")) {
    return nextResolve("./identity.ts", context);
  }
  return nextResolve(specifier, context);
}
