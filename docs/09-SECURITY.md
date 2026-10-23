# Good Showroom AI Chatbot - Security

Purpose: Set enforceable security rules for Codex and future implementation.

Status: Approved by User  
Updated: 2026-10-22  
Approval: The user approved Security on 2026-10-22 and authorized the Test Strategy draft only. No coding or key/database/configuration operations are authorized.
Basis: Approved [PRD](01-PRD.md), [Scope](02-SCOPE.md), [User Flows](03-USER-FLOWS.md), [Architecture](04-ARCHITECTURE.md), [Tech Stack](05-TECH-STACK.md), [Database Schema](06-DATABASE-SCHEMA.md), [API Contracts](07-API-CONTRACTS.md), and [UI/UX](08-UI-UX-GUIDELINES.md). Rules and numeric settings below are approved designs; remaining open values are explicit. No controls have been implemented or tested.

## 1. Security Policy and Trust Boundaries

**SEC-01. Verify each access; deny by default.** Browser state, local network location, Google login, administrator status, model output, and database content grant no implicit trust. Authenticate the caller and authorize the exact action/resource at each protected boundary. Missing, expired, conflicting, or unavailable policy must release no protected data. This follows [NIST zero-trust principles](https://csrc.nist.gov/pubs/sp/800/207/final).

Keep the approved two services and one agent. Next.js verifies browser identity; FastAPI independently verifies server identity and current permissions. Tools and repositories accept explicit validated authorization context, never ambient user IDs or caller-supplied grants. Pure calculations/formatters accept only already-scoped data; they cannot fetch broader records. Every protected service/tool/repository method checks its required scope even when called internally.

This is a security specification, not production certification. Existing broad MongoDB grants and development-only loopback HTTP are explicit limitations. Host/key compromise can bypass application protections; no device-attestation, database least-privilege, or tamper-proof audit guarantee is claimed. No new infrastructure or credential changes are prerequisites for project work.

## 2. Google Identity, Tokens, and Keys

**SEC-02. Bind identity, not display email.** Use the selected NextAuth V4 Google flow with its state/PKCE protections and verified provider identity. Check provider issuer/audience/expiry and `email_verified`; bind Google's stable provider ID to the immutable app user. No email-only account linking, Gmail alias rewriting, source-password reuse, or browser-supplied role claims. Request only identity scopes; do not retain Google access/refresh tokens unnecessarily. Follow [NextAuth Google guidance](https://next-auth.js.org/providers/google).

Bootstrap the configured trusted email once inside the app transaction guard. Preapproval consumes once with explicit role/scope. Preserve the last active admin under concurrent changes; ordinary users cannot self-approve. Deny self-escalation unless a separately approved rule permits it. Absolute app-login lifetime: eight hours, capped by NextAuth expiry; cookie refresh cannot extend that binding. Require a new verified sign-in after expiry. Recommend Google MFA for administrators; ordinary Google OAuth does not prove MFA enforcement.

**SEC-03. Server-only identity assertions.** Approved token profile:

| Item | Required rule |
|---|---|
| Signature | `RS256`, 3072-bit RSA; Next.js holds the private key, FastAPI only public verification keys. Reject other algorithms, `none`, unknown `kid`, and token-selected key URLs. |
| Claims | Require fixed environment-specific issuer/audience, purpose, `iat`, `nbf`, `exp`, `jti`. Normal requests also require app `sub`, login-session ID/generation. Bootstrap uses a distinct purpose/profile, verified provider identity, and originating login binding. |
| Lifetime | Maximum 120 seconds; clock tolerance 30 seconds. Reject future claims outside tolerance and overlong assertions; tolerance never extends backend login/lease expiry. |
| Binding/replay | Bind assertions to HTTP method, normalized route, and digest of forwarded query/body. Exchange uses a server-minted login UUID as its durable creation/replay identity; repeated exchange returns the existing binding, never renews/recreates a revoked one. Retain revoked replay bindings through assertion expiry/tolerance; do not shorten their original expiry on logout. Normal retries use API idempotency/version checks. `jti` alone is not replay prevention. |
| Rotation | Maximum key age 90 days. Retain previous public keys for at least 210 seconds after last issuance (token lifetime + tolerance + turn deadline). Revoke a compromised key immediately; invalidate affected login generations/in-flight work. No grace period for compromise. |

Validate signatures/required claims through maintained `jose`/PyJWT APIs with a fixed algorithm/key mapping; never trust decoded-but-unverified payloads. Bound token/header size. Bind ongoing execution to current login/policy and revoked-key state before release. Key handling follows [JWT best practices](https://www.rfc-editor.org/rfc/rfc8725.html). Design approval does not authorize generating keys or editing configuration now.

## 3. Authorization and Aggregate Disclosure

**SEC-04. Central RBAC + ABAC.** Role permissions define the maximum; verified user/site/ownership/assignment/record/field attributes narrow access. Deny overrides allow. Independently check tool, collection, joined records, projected fields, metric/grouping, saved-chat owner, and admin-review capability. Selected IDs and cursors are selectors, never proof of permission. Avoid object-existence leaks using the approved safe errors. See [OWASP authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html).

Load current policy at entry and recheck at tools, commit, and release. Changes invalidate stale evidence/context/cursors. Access/admin changes compare expected versions and share the transaction fence/audit. No admin grant exceeds the server-defined grant-management authority; admin settings do not imply access to all business records.

**SEC-05. Authorize records before aggregation.** Sensitive groups require an approved per-metric disclosure policy covering minimum cohort, complementary totals, overlapping filters, repeated differencing, history, and visuals. A threshold alone is insufficient. Until approved, withhold affected breakdowns; offer only an independently safe permitted result. Do not invent a threshold or persist Temporary Chat queries as a disclosure ledger. Undefined rules disable the affected disclosure, not project work.

## 4. Operational Reads and AI Boundaries

**SEC-06. Read-only through application code.** Keep existing credentials/grants unchanged. OperationalReadRepository exposes named reads only against `byd-leads-new`/`byd-panel`; necessary app writes target only `byd_ai_layer`. Supply no raw client/collection/command escape hatch to tools. Trusted builders recursively allowlist operators, stages, joins, fields, and sorts. Bound fields/rows at the query boundary, not just after fetching. Forbid insert/update/delete/replace/upsert, `$out`, `$merge`, `$where`, `$function`, `$accumulator`, arbitrary regex/code, and unapproved nested collections. Never merge untrusted JSON into a query. [OWASP NoSQL guidance](https://cheatsheetseries.owasp.org/cheatsheets/NoSQL_Security_Cheat_Sheet.html) informs validation; current credentials do not enforce these restrictions if code is bypassed or credentials compromised.

**SEC-07. The LLM has no security authority.** Treat prompts, retrieved notes, tool output, old messages, and summaries as untrusted data. Separate them from instructions; minimize fields before model input. No embedded instructions can change tools/grants/scope. Expose no shell, arbitrary HTTP/file execution, generated Mongo queries, or new hosted tools. MetricRegistry calculates; ResponseBuilder validates evidence, numerical claims, and the shared text/visual dataset. Reject unsupported claims; never stream unvalidated results. Follow [OWASP agent guidance](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html).

## 5. Browser, Transport, and Input Protection

**SEC-08. Protect every route.** Only fixed Next.js proxy routes reach loopback-bound FastAPI. No wildcard CORS, generic proxy, trusted browser identity headers, or user-selected provider/DB URLs. Verify configured Host/Origin; do not trust arbitrary forwarded-host headers. Google/OpenAI/Mongo connections require TLS with certificate validation. HTTP is an explicit development exception only for loopback; any non-loopback use needs separate transport/security review.

NextAuth cookies use HttpOnly, host-only scope, SameSite=Lax and Secure on HTTPS. Preserve library login/logout protections. Custom cookie-authenticated mutations require a verified, session-bound CSRF token plus exact same-origin validation; reject missing/mismatched checks. No state-changing GET routes. [NextAuth options](https://next-auth.js.org/configuration/options) and [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html) are the baseline.

**SEC-09. Strict parsing and safe rendering.** Reject unknown input fields, invalid types, excessive nesting/arrays, nonfinite numbers, and malformed IDs. Caps: 64 KiB request body before parsing; 1 MiB encoded response and each stored app document. Reject oversized turns before commit, never silently truncate facts. Preserve API query/page/tool/token/rate limits; one active turn per chat, bounded retries/cancellation, admission before paid work. Authenticate/rate-limit access-request/admin routes too; rate controls do not replace authorization.

Escape text by context. Approved Markdown uses restricted elements, no raw HTML/MDX/images/iframes, and validated links. Trusted chart components use allowlisted bindings/style tokens, never model-provided code/CSS. Set `no-store`, `nosniff`, no-referrer, and a restrictive Content Security Policy with frame/object blocking and controlled connect sources. Outside development, scripts require nonces/hashes; no blanket `unsafe-inline`/`unsafe-eval` script policy. Never expose internal tokens, traces, or sensitive payloads in URLs, browser storage, analytics, or debug pages. Library defaults are not a security test.

## 6. Saved and Temporary Conversation Protection

**SEC-10. Authoritative validated commits.** Follow Schema's app-only transaction/fence, unique request/sequence keys, version checks, and final access recheck. No permanent SDK session auto-save or provider continuation. Unknown commit means reconcile before retry, not success. Reopening history rechecks grants over messages, evidence, summaries, and visuals. Unsafe old items are omitted/rebuilt. Admin review is separately authorized/audited and never impersonates the owner.

**SEC-11. Temporary means no saved transcript.** Keep contents, IDs, hashes, locks, evidence, and retries in process/browser working memory only. Bind to user plus specific login generation. Enforce the approved 30-minute idle expiry, five-minute maximum login lease, and maximum 60-second cleanup sweep. Renew only after fresh NextAuth verification; never beyond auth expiry or after revocation. Protected entry/commit/release checks enforce expiry without waiting for cleanup.

End/logout/expiry/restart cancels work and invalidates context; late completion cannot restore it. Logout coordinates backend revocation with sign-out. If notifications fail, deny reuse at lease expiry and sweep abandoned memory within a further minute while the process operates. Immediate remote deletion is not guaranteed if delivery fails. Minimize references; disable content-bearing crash/debug dumps. Do not claim secure RAM erasure or protection from host swap/compromise. Clear browser state and disclose cleanup uncertainty.

## 7. Traces, Logs, and Provider Privacy

**SEC-12. Native developer tracing only.** Explicit `OPENAI_TRACING_ENABLED=true` plus `APP_ENV=development` is required; missing/invalid configuration or other environments means off without separate approval. Map app flags explicitly; force `trace_include_sensitive_data=False` for real/temporary data and set `RunConfig.tracing_disabled`/global controls consistently before runs.

Replace default processors at startup with `set_trace_processors`; one native batch processor uses a schema-compatible allowlisting exporter before `BackendSpanExporter`. No parallel default exporter. Permit only random diagnostic IDs, static workflow/tool/stage names, fixed outcome/error enums, duration and token counters. Exclude chat/customer/business values, emails, source record/auth/temp IDs, raw arguments/results, response bodies, URLs/secrets, exceptions/stack traces, and provider response IDs not required by this allowlist. Drop unknown fields/unsafe batches. Sanitization/export/flush failure drops diagnostics, never breaks chat or bypasses a real security failure.

OpenAI project/dashboard access is developer-only; app administrators get no automatic access or trace endpoint. Verify project access/retention and exporter compatibility before real-data export; unresolved settings leave export off, not chatbot execution blocked. `store=false` on supported foreground model requests is separate from traces, abuse monitoring, and other provider retention. Temporary mode can leave permitted diagnostic metadata; promise no zero provider retention. Checked against [official tracing guidance](https://developers.openai.com/api/docs/guides/agents/integrations-observability), SDK source through Context7, and [provider data controls](https://developers.openai.com/api/docs/guides/your-data).

**SEC-13. Minimal auditable events.** Record access changes/revocations, admin review, denials, and security outcomes using opaque actor/target IDs and fixed categories. Required security-change audit failure aborts its transaction; required review audit failure denies review. No transcripts, temporary identifiers/query hashes, business values, or raw HTTP bodies/paths in logs/audits. Disable or sanitize framework/auth/driver/SDK debug and access logs before serving; use static route templates. Saved deletion covers derivatives; TTL is not authorization or cascade deletion. Audit immutability is application-enforced, not protected against a compromised app DB writer.

## 8. Secrets, Incidents, and Verification

**SEC-14. Protect secrets and dependencies.** Preserve ignored `.env` values; templates contain placeholders only. Keep Google/OpenAI/Mongo secrets, NextAuth secret, signing private key, and separate cursor/CSRF keys server-only and purpose-separated, with restrictive file access. No secrets in `NEXT_PUBLIC_*`, prompts, examples, commits, or exception output. Authenticated encrypted cursors bind caller/filter/policy/expiry; accept no executable query state. Critical config failures deny protected actions. No key-generation/configuration change is performed by this draft.

Pin reviewed stable dependencies/lockfiles and review copied shadcn/plugin code. Check advisories and static/security findings during implementation; fix exploitable trust-boundary issues before claiming readiness. Never bypass peer/security checks to force installation. Use only the approved stack; add no monitoring, authorization platform, queue, or service.

**SEC-15. Contain and prove.** On suspected compromise, stop affected requests/export, revoke sessions/keys/access, and preserve sanitized evidence. Coordinate any operational credential response with the owner; this document authorizes no automatic rotation/deletion. Notify the project owner privately, without placing secrets in reports. Resume only after reviewed fixes and verified controls.

Later tests must demonstrate forged/wrong-purpose/replayed token rejection, CSRF/CORS/IDOR denial, tenant/site/field/join isolation, concurrent admin/revocation guards, mutation prevention, stored prompt injection/XSS, metric/visual consistency, byte/rate bounds, atomic retry recovery, temporary non-persistence, and trace/log sentinel exclusion. Use isolated fixtures/mocks/test databases, never production mutation tests. Codex must report commands/results and residual risks honestly; no security assurance comes from this document alone.

## 9. Review Decisions and Scope

The crypto profile, rotation/tolerance, eight-hour absolute login, byte caps, and browser defenses are approved design requirements. Saved/audit retention and deletion periods, aggregate disclosure/differencing rules, administrator MFA assurance, exact CSP/CSRF configuration, and provider project access/retention still need confirmation or implementation verification. No automatic content TTL or sensitive breakdown is enabled from an unreviewed placeholder. These decisions do not require new Atlas credentials or block documentation/local development.

[Tickets.md](../Tickets.md) remains authoritative; existing architecture/security/evaluation tickets stay Backlog. Test Strategy is the current authorized draft. Stop for its review before the next document; do not install dependencies, provision keys, change databases, or start coding yet.
