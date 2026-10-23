# Good Showroom AI Chatbot - Product Requirements Document

Purpose: Define the read-only chatbot from the user's brief, selected directions, and repository findings.

Status: Approved by User  
Updated: 2026-10-22  
Approval: Approved on 2026-10-22. Subsequent user-confirmed directions are recorded below. Open technical decisions remain explicit; this approval does not start implementation.

Change record (2026-10-22): The user explicitly requested application-enforced operational reads with unchanged credentials, opt-in developer-only tracing, application-owned validated-turn commits, metric definitions, and evidence-backed responses. The amendments below implement that direction; see [Architecture](04-ARCHITECTURE.md) and [DECISIONS.md](DECISIONS.md). Other approved product requirements are preserved.

## 1. Product Goal and Boundary

Build an independent AI backend that answers staff questions through controlled reads of two existing operational MongoDB databases. These databases are the knowledge source; the cloned backends provide schema/business context, not runtime API dependencies. Send the model only necessary authorized results.

The wider ecosystem includes leads, sales, delivery, accounting, marketing, workshop, parts, suppliers, and trade partners. Expose only verified data and supported metrics. All project changes stay in `goodshowroom-ai-layer/`; exclude `booran-sales-floor-backend`.

## 2. Users, Login, and Existing Systems

Administrators manage roles and employee access. Managers/employees receive only admin-granted information. Map AI users to operational employees before offering "my leads."

1. Sign in with Google through NextAuth in Next.js. Verify identity server-side; FastAPI must validate a trusted identity/session contract, not browser-supplied roles or email.
2. An active email preapproval with an assigned role/scope skips the request screen after Google verification. Bind the account to Google's stable provider identifier.
3. Other authenticated users request access. Pending, rejected, or revoked users see only request/status screens.
4. Administrators approve/reject, assign access, preapprove emails, and revoke access. Prevent self-approval; audit changes and enforce current grants.

Bootstrap the first administrator from one configured trusted Google email. Support multiple administrators thereafter; only an existing administrator can grant administrator access. The exact initial email and account-ownership policy remain to be configured. Google login alone grants no business access.

| Reference repository | Verified code responsibility |
|---|---|
| `byd-leads-backend` | Express/Mongoose: leads, dealerships, inventory, appointments, conversations, manual SMS, imports. |
| `byd-sales-floor-backend` | Express/Mongoose: customers, opportunities, allocations, stock holds, sales logs, delivery handoffs; uses lead/delivery databases. |
| `shaun-backend` | FastAPI/MongoDB: delivery clients, documents, offers, inspections, contractor jobs/time/evidence, Virtual Yard deals. |

Full accounting, parts/workshop, marketing, supplier/trade portals, and Pentana integration were not established in these repos. A read-only inspection on 2026-10-22 verified both operational connections and sampled their live structure; see the evidence and limits in `02-SCOPE.md`.

## 3. Version 1 Requirements

| ID | Requirement |
|---|---|
| PRD-01 | Verify NextAuth identity and active approval. Apply current admin grants before database queries; default to no access. |
| PRD-02 | Use one orchestrating agent, typed tools, and controlled MongoDB reads. Validate contracts; bound time, pagination, and results. No arbitrary model-generated queries, collections, or code. |
| PRD-03 | Preserve relevant question state across turns; clarify ambiguity and refresh permissions/business facts. |
| PRD-04 | Define enabled metrics in a trusted MetricRegistry and calculate them deterministically in code. Never invent figures or reuse example/fallback values. |
| PRD-05 | Use an evidence-backed ResponseBuilder for structured status, answer, authoritative facts/data, visual specification, sources, period/timezone/filters, freshness, coverage, and warnings. |
| PRD-06 | Persist saved sessions and app data in `byd_ai_layer`. Users can reopen their own saved chats; administrators can review all saved user conversations. Temporary chats have no saved transcript or later administrator review. Apply retention/deletion and current permissions to saved history. |
| PRD-07 | Provide Google login, access-request/status screens, responsive chat/history/session selection, Temporary Chat, administrator saved-chat review, loading/errors, and in-chat visuals. Streaming is optional. |
| PRD-08 | Use modular code, dependency injection, replaceable data adapters, and minimal model calls. Do not change or run reference backends. |
| PRD-09 | Provide admin-only access settings: data domains, databases, sites, assigned/selected/all-permitted records, and sensitive fields. Audit changes. |
| PRD-10 | A backend AuthorizationService combines roles and attributes to enforce database, collection, record, field, aggregate, and session permissions independently of the LLM, including policy-version rechecks before output. |
| PRD-11 | Render matching in-chat cards/charts/tables from the answer's authorized dataset. Downloadable reports/files are excluded. |

Candidate questions, enabled only after verification: period-based lead counts; assigned/pending leads and follow-ups; lead statuses/sources; salesperson or permitted dealership comparisons; this month's versus last month's leads; converted leads with linked sales; pending deliveries/customer status. Inventory/contractor reads depend on verified support.

## 4. Reporting Policy

- **Approved calendar-day rule:** "last seven days" means the previous seven completed dealership calendar days, from local midnight seven days ago (inclusive) to today's midnight (exclusive). "Today" runs from local midnight to now. Explicit past-168-hour requests use a rolling window.
- **Timezone:** verified dealership timezone; current source default is `Australia/Melbourne`. Query boundaries convert to UTC. Cross-site reporting timezone remains open.
- **Lead received:** original source creation time, or verified native creation time. Never silently substitute import, scrape, or test-drive dates.
- **Coverage:** disclose uncertain/unclassified dates, incomplete results, and truncation. Verify identities, deduplication, archived-record exclusions, lifecycle definitions, and conversion links before enabling metrics.

## 5. Role-based Guardrails and Data Safety

Resolve roles and admin-selected restrictions from server records. The role may permit either operational database, both, or selected information within them. Enforce collection/site/ownership/selected-record filters before retrieval and aggregation; project only permitted fields. Permissions in chat text or saved context cannot grant access.

Use deterministic server checks at every tool boundary; model/input guardrails are supplementary. Validate results and current permissions before releasing or streaming protected content. Missing grants or unavailable policy checks deny access. Suggested response: "You do not have access to this information. Please contact an administrator." Do not reveal restricted existence, counts, or internal database names.

Operational databases remain application-level read-only: no record mutations, appointments/transactions, messages/emails, imports, or workflow execution. Keep existing credentials/permissions unchanged. A dedicated operational read repository exposes only approved reads; trusted code constructs and authorizes all queries, nested pipelines, joins, projections, and limits. Forbid mutations, write stages, arbitrary commands/operators, and server-side JavaScript. Both supplied operational connections report `readWriteAnyDatabase`; protection is application-enforced and cannot be guaranteed if code is bypassed or credentials are compromised. No restricted-user setup or rotation is required. Necessary users/roles/settings/history/audit writes target only `byd_ai_layer`; provisioning remains unverified.

Treat retrieved content as untrusted data. Keep credentials and restricted fields outside model prompts, outputs, and logs. Business write actions are deferred; any future write requires preview and explicit confirmation.

## 6. Session Context Pipeline

Design: **persist validated completed turns, maintain validated question state, and send bounded, permission-filtered context each turn**. Application-owned ChatRepository is authoritative. Prepare history explicitly and run without automatic SDK session persistence or provider-managed continuation, so intermediate/unvalidated items are never automatically saved. Use one history strategy.

| Step | Behavior |
|---|---|
| 1. Verify | Check identity, approval, current grants, and session ownership or explicit administrator history-review access before loading saved history. A session ID alone grants nothing. |
| 2. Load | Read only this user's chosen session. New sessions start clean; no automatic memory sharing across users/sessions. |
| 3. Recheck | Filter history, summaries, tool evidence, and visuals against current grants. After permission changes, rebuild or omit context that cannot be safely filtered. |
| 4. Resolve state | Retain metric, authorized entities, filters, timezone, absolute dates, and unanswered questions. "What about last month?" updates dates while preserving relevant filters. |
| 5. Build | Combine trusted instructions, allowed tools, validated state, older-turn summary, recent complete turns, and the question. Respect token budgets, preserve tool-call/result pairs, and never trim mandatory policy. |
| 6. Retrieve | Check every typed tool call, read fresh permitted data, and validate results. Earlier answers/summaries cannot establish current metrics. |
| 7. Respond | Compute numbers; verify answer/visual agreement, freshness, and access before release. Recheck policy changes during a run. |
| 8. Save | In saved mode, atomically commit the validated user/assistant turn, state, response/visuals, and permitted evidence only after final access checks. Request IDs/version checks handle duplicates, interruption, and stale updates. Temporary chats bypass persistent chat storage. |

Retain recent turns intact. Summarize older authorized turns only when the context budget requires it; keep essential filters in validated state. Failed summarization falls back to safe recent context/state and clarification, not invented memory. Preserve the retained transcript.

Attach source/filter/date references, retrieval time, and permission/context versions to saved evidence. Use visual references rather than replaying large chart payloads. Store no credentials, raw reasoning, or database dumps. Retention/deletion covers messages, state, summaries, tool evidence, and visuals. Detailed schemas and budget/retention limits follow later.

Administrators may read all saved user messages. Record administrator review and clearly disclose it to employees. This does not grant access to another user's live session or enlarge operational-data permissions. Recheck protected answer/tool/visual data against the reviewer's current grants; do not copy another user's history into the administrator's agent context by default.

Temporary Chat is private from saved history and later administrator review. Keep its context only in active process/browser memory; discard it on end, logout, session expiry, or restart. Do not persist its messages, summaries, query state, tool results, or visuals in app databases, browser persistent storage, logs, tracing, or analytics. Minimal security records contain no message content. Explicitly enabled developer traces may retain permitted sanitized diagnostic metadata, not a transcript; provider tracing and model-response storage are separate. Switching modes starts a separate chat. Cleanup failure/lease bounds and provider retention must be disclosed accurately.

## 7. Structured Responses and Visual Artifacts

Render the typed response as readable text and approved interactive components. All values must agree with the same authorized dataset.

| Data type | Default presentation |
|---|---|
| Single metric | Card with value, unit, and period. |
| Categories/comparisons | Bar chart with clear labels and values. |
| Dated trend | Line chart when enough ordered observations exist; never invent points. |
| Record list | Table with permitted columns, pagination, and coverage. |
| Denied/unavailable/unsupported/failed request | Structured status message; no fabricated chart. Distinguish verified zero from missing data. |

Use deterministic visual-selection rules and validated numbers/labels. Render approved specifications rather than executing model-generated HTML/JavaScript. Provide responsive layouts, units, and accessible table/text alternatives. Saved visuals retain owner/session, scope, source, and freshness metadata. Recheck access when reopening them; historical visuals are dated snapshots.

## 8. Technology and Exclusions

Stack: Python/FastAPI, OpenAI Agents SDK, configurable OpenAI model, Pydantic, async MongoDB, Next.js/TypeScript/Tailwind, and NextAuth Google login. Pin compatible stable versions. Database access belongs in typed backend services, never the browser/model.

Keep MongoDB URIs, provider credentials, and `AUTH_SECRET` in the ignored `.env`. Configure NextAuth's local URL/callback before login testing. No old-backend API keys are needed; configuration presence does not prove validity/connectivity.

Local development/testing only. Exclude deployment, Docker, Kubernetes, CI/CD, cloud provisioning/hosting, external monitoring platforms, OpenTelemetry infrastructure, and alerting. The user explicitly permits native OpenAI Agents SDK tracing for authorized developers when configured: sensitive capture off by default, safe correlation/diagnostic metadata only, no employee/admin trace endpoints. Outside development export stays off without separate approval. Keep `store=false` on supported model requests; it does not disable SDK trace export or guarantee zero provider retention. Ordinary logs contain no raw chat/customer content.

## 9. Known Gaps

Source paths are workspace-relative. Code review is supplemented by the bounded read-only inspection recorded in `02-SCOPE.md`.

| Gap | Current position |
|---|---|
| G1 - Access | NextAuth/admin approval selected; server identity verification, role guardrails, and employee mappings need implementation. |
| G2 - Dates | Direct reads bypass API filter limits. Imports substitute scrape/test-drive dates: `byd-leads-backend/seed/seed-virtualyard.js:187`, `:255`. Verify timestamp meaning. |
| G3 - Accuracy | Avoid fixed API fallbacks: `byd-leads-backend/routes/dashboard.js:118`; Sales Floor `src/services/crmService.js:4334`. Raw data/metric definitions still need verification. |
| G4 - Relationships | Access grants do not link records. Live inspection matched all 354 opportunity customer references, but only 349 of 351 populated opportunity delivery references. Employee IDs remain sparse/inconsistent; verify mapping before "my leads." |
| G5 - Read effects | Avoid write-producing GETs (`byd-leads-backend/services/threadHistory.js:74`). Keep broad existing credentials unchanged; enforce controlled reads in the application and test that the operational repository exposes no writes. |
| G6 - Legacy secrets | Cleanup deferred by user; no other-repo changes. Protect this application's secrets. |
| G7 - Readiness | Partly resolved: both databases connected; 40 collection inventories/index lists inspected; field types sampled from up to 100 documents per collection; lead-date coverage and selected CRM links checked. Remaining: timestamp provenance, staff/site mapping, edge cases, and query performance. Existing credential permissions are an acknowledged application-boundary limit, not a setup blocker. Sampling does not validate every record. |

## 10. First Milestone and Verification

An approved Google user asks for the last seven days' lead count and receives an accurate, permitted answer and metric card, with persistent follow-up context.

- [ ] Admin approval/preapproval assigns access; unauthorized settings changes/self-approval are rejected.
- [ ] NextAuth/FastAPI identity, role, database, record, field, and owner/session checks reject forged, expired, unauthorized, or revoked access.
- [ ] Typed read tools return deterministic counts under approved date/coverage rules; operational writes are blocked.
- [ ] Answer/card agree and state source, period, timezone, scope, and limitations.
- [ ] Session resume, follow-ups, long-history summaries, retries/concurrent turns, and saved visuals preserve context and current permissions.
- [ ] Correctly handle invalid dates, zero matches, uncertain/incomplete data, unsupported questions, source failure, and failed summaries.
- [ ] Tests cover injection, safe rendering, unauthorized sessions/data, revoked historical context, and app/source storage separation.
- [ ] Multiple administrators are supported after first-admin setup. Administrators can review saved messages; employees cannot review each other's chats. Temporary transcripts/context are not saved or later reviewable, including after retries/errors.
- [ ] Reproducible evaluations cover metric evidence, prompt injection, mutation prevention, atomic commits, permission changes, and temporary isolation. Configured developer traces exclude sensitive content and do not affect chat success if export fails.

Use Pytest, deterministic fixtures, mocked database adapters, a separate test database, integration tests, agent evaluations, and end-to-end verification. Ordinary tests require no production database. Label mocks; they do not prove real integration. Accuracy and access isolation take priority.

## 11. Open Decisions and Review

| Decision | Still required |
|---|---|
| D1 - Metrics | Calendar-day boundaries approved. Finalize exclusions, deduplication, source timestamp reliability, and conversion meanings. |
| D2 - Access | Finalize role/individual precedence, database/record/field options, and employee mappings. Admin review of saved messages and private Temporary Chat are selected. |
| D3 - Setup | One configured trusted email bootstraps the first of potentially multiple admins. Finalize identity-token keys/rotation, account ownership, callback, and logout/session lease. MongoDB credentials/grants remain unchanged. |
| D4 - Release | Scope's five question families are approved; delivery/converted-lead questions remain conditional. Metric-specific business definitions still require verification. |
| D5 - Context | Set context budgets, saved-chat retention/deletion, temporary-chat lifecycle/cleanup/provider disclosure, schemas, and `byd_ai_layer` placement. |
| D6 - Targets | Latency, result-size/model-cost limits, and cross-site reporting timezone. |
| D7 - Visuals | Later UI/API review selects components/library, accessibility, and bounded datasets. In-chat only; no exports. |

Sources: user brief/revisions; [ecosystem schematic](<../../Good Showroom Ecosystem Schematic.pdf>); reference snapshots `byd-leads-backend@cd77ee8`, `byd-sales-floor-backend@da873b3`, `shaun-backend@fcefa0d`. `mongodb.txt` supplies configuration only; credentials are withheld.

Official references: [Google verification](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token), [NextAuth Google](https://next-auth.js.org/providers/google), [NextAuth configuration](https://next-auth.js.org/configuration/options), [OpenAI session memory](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory), [tool guardrails](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals), and [history strategies](https://developers.openai.com/api/docs/guides/agents/running-agents). The context pipeline is an application-specific recommendation, not an implemented SDK default.

PRD, Scope, and User Flows are approved. The current user request authorizes architecture amendments and corresponding Backlog implementation tickets now; this is an explicit exception to later ticket-generation timing. Architecture remains the current document for review. Do not start application implementation or Tech Stack automatically.
