import "server-only";

/** Validate profile data after NextAuth has verified the Google response.
 * This function does not verify an ID token or authenticate the caller.
 */
export function googleIdentityFromVerifiedCallback(
  provider: unknown,
  providerAccountId: unknown,
  profile: unknown,
): { provider: "google"; provider_account_id: string; email: string; email_verified: true } {
  if (provider !== "google" || typeof providerAccountId !== "string" ||
      providerAccountId.length < 1 || providerAccountId.length > 255 ||
      typeof profile !== "object" || profile === null || Array.isArray(profile)) {
    throw new Error("Identity verification failed.");
  }
  const value = profile as Record<string, unknown>;
  if (value.sub !== providerAccountId || value.email_verified !== true ||
      typeof value.email !== "string" || value.email.length > 254 ||
      !/^[^\s@]+@[^\s@]+$/.test(value.email)) {
    throw new Error("Identity verification failed.");
  }
  return { provider: "google", provider_account_id: providerAccountId, email: value.email, email_verified: true };
}
