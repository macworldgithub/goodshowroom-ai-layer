# Architecture Decisions

Purpose: Record user-directed project decisions and their limits.

Status: Active - Documents 01–10 and Implementation Plan Approved; Ticket Refinement Awaiting Review

Record policy: Earlier entries preserve chronology. The latest architecture-revision entry below supersedes earlier recommendations for new credentials, a permanent SDK session adapter, and tracing disabled for the whole release. It also clarifies metadata-only temporary diagnostics. Those earlier recommendations are not current implementation instructions.

## 2026-10-22 - Independent backend and direct database reads

The user selected an AI backend that reads existing operational MongoDB databases directly. The cloned backends provide schema and business context; they are not runtime API dependencies. Make changes only inside `goodshowroom-ai-layer/`. Do not repair or change the other repos.

Operational data remains read-only. Use controlled typed queries; read-only database grants are recommended. The subsequent inspection and user-directed deferral are recorded below.

## 2026-10-22 - Administrator-managed access

The user selected an administrator settings interface for creating/updating roles and assigning employee access. The AI backend must enforce these grants on each query. Exact permission options and user-to-operational-employee mappings remain open in the PRD.

## 2026-10-22 - Google login with administrator approval

The user selected NextAuth in Next.js for Google login. Authenticated users without approved access see a request-access screen. Administrators approve/reject requests and assign roles/scopes. Administrators can preapprove an email with a role/scope so its verified Google account skips the request screen. Google authentication alone grants no business access. Support revocation and audit access changes.

Verify identity server-side and bind approved accounts to Google's stable provider account identifier. Do not assume a NextAuth cookie or browser-supplied role is sufficient identity proof for FastAPI. The initial-admin direction is recorded below; email-ownership/account policy and the trusted Next.js-to-FastAPI session contract remain open.

## 2026-10-22 - Separate AI application storage

The user selected a separate database for conversation history. The configured technical name is `byd_ai_layer`; it has not been provisioned. Keep AI application data and its write credential separate from operational data. Retention and connection placement remain open.

## Current release boundary

The user selected read-only business operations. Business write actions remain deferred. Any future business write requires a proposed-change preview and explicit user confirmation; no such tools are authorized for this release.

## 2026-10-22 - Role guardrails and per-user session context

The user confirmed role/admin-selected access to the two operational databases and permitted employee records. Enforce this in server-side checks on every database tool call, including collection, site, record, field, and aggregate scope. Missing access returns a clear denial without restricted data. Model guardrails supplement these checks; they cannot grant access.

The user confirmed conversation history per user and session in the AI database. The PRD records current access checks, validated query state, bounded recent turns, budget-triggered summaries, fresh authorized results, and versioned persistence. Subsequent approval, administrator review, and Temporary Chat decisions below update the original owner-only history direction. Detailed architecture/schema documents remain unstarted.

## 2026-10-22 - Structured responses and in-chat visuals

The user requested structured answers and visual artifacts suited to the returned data, and selected in-chat charts, cards, and tables for version 1. Exports/downloadable reports are excluded. Written answers and visuals must use the same validated, authorized dataset. Persisted visual results remain bound to user/session ownership, current permissions, and source freshness. Detailed response schemas and rendering components await later review.

## 2026-10-22 - PRD approval and two-document drafting exception

The user explicitly approved `01-PRD.md` and authorized drafting `02-SCOPE.md` and `03-USER-FLOWS.md` together. Both new documents remain drafts for review. This is a specific exception to the one-document drafting sequence; later documents still require review authorization. No application implementation or actual tickets were authorized in this step.

The user approved the recommended calendar-day reporting rule. "Last seven days" means the previous seven completed dealership calendar days. Source timestamp reliability, exclusions, deduplication, and metric meanings still need technical validation.

## 2026-10-22 - Multiple administrators and question families

Use one configured trusted Google email for the initial administrator. Support additional administrators granted by existing administrators; prevent ordinary self-approval and preserve an active administrator. The user requested draft first-release questions governed by assigned access. Scope includes lead period counts, status/source breakdowns, assigned/pending lists, and permitted group comparisons; details await Scope review.

## 2026-10-22 - Saved-message review and private Temporary Chat

The user confirmed that administrators can see users' saved conversations. Employees may reopen their own saved chats, but not other employees' chats. Administrator review is distinct from operational data grants; record review access and disclose review to employees. Protected business facts/visuals still require the reviewer's current grants.

The user selected Temporary Chat: messages/context exist only during the active chat, then are discarded. No saved transcript or later review exists for users or administrators. Do not persist temporary messages, summaries, state, tool evidence, or visuals in app databases, persistent browser storage, content logs, tracing, or analytics. Minimal security records contain no message content. Saved and temporary modes use separate sessions and the same data permissions. Provider-side retention and technical cleanup limits must be stated accurately in later design documents.

## 2026-10-22 - Read-only database discovery and deferred Atlas changes

Both supplied operational connections succeeded: `byd-leads-new` (9 collections) and `byd-panel` (31 collections). Collection/index inventories, bounded type samples, full lead-date coverage, and selected CRM identifier relationships were inspected without modifying operational data. Evidence and sampling limits are in `02-SCOPE.md`; this does not constitute application testing or complete schema validation.

Both operational connections report `readWriteAnyDatabase`. Recommended hardening is a dedicated reader limited to the two operational databases and a separate writer limited to `byd_ai_layer`. The Atlas connector requires reauthentication, so it cannot provision accounts currently. The user then explicitly directed leaving the MongoDB Atlas connection/credentials as-is and avoiding extra prerequisites before project work. Stop account/Atlas setup, preserve existing credentials/backend accounts, and keep hardening deferred without blocking documentation/local development. Controlled reads remain mandatory; do not claim database-enforced read-only grants. No database records, indexes, users, or permissions were changed.

## 2026-10-22 - Scope and User Flows approved; Architecture drafted

The user approved `02-SCOPE.md` and `03-USER-FLOWS.md` and requested only `04-ARCHITECTURE.md` next. Their approval metadata is updated; the Architecture draft awaits review. Do not treat its new technical proposals as approved decisions or proceed to Tech Stack/implementation without authorization.

The draft proposes two local services, a signed NextAuth-to-FastAPI identity handoff, current server-side role/scope checks, controlled data adapters, an application-owned Mongo session adapter, and a separate memory-only Temporary Chat adapter. A narrowing permission rule, one-process temporary storage with 30-minute idle expiry, complete rather than streamed responses, and tracing disabled from startup are specifically proposed for review. No architecture implementation, credentials/Atlas changes, or further database inspection occurred in this documentation step.

## 2026-10-22 - Explicit Architecture Revision Request

The user explicitly directed the following requirements. They supersede conflicting earlier technical recommendations; PRD, Scope, and User Flows contain visible amendment notes. The revised Architecture document awaits review as a whole. No implementation or next phase is authorized.

### Operational reads and unchanged credentials

Keep current MongoDB credentials/permissions unchanged. OperationalReadRepository exposes only approved read methods/collections and trusted query builders; reject mutations, aggregation writes, arbitrary commands/operators, server-side JavaScript, unauthorized joins, and driver escape hatches. Bound queries/results/projections/pagination. Necessary application writes remain in `byd_ai_layer`. Protection is application-enforced, not credential-enforced; bypassed code or compromised credentials are outside that guarantee. No database user setup/rotation requirement is added.

### Developer tracing and privacy amendment

Permit explicitly configured native OpenAI Agents SDK export for authorized developers, with sensitive capture off by default and enforced off for real/temporary content. `.env.example` records APP_ENV, OPENAI_TRACING_ENABLED, and OPENAI_TRACING_INCLUDE_SENSITIVE_DATA; the actual `.env` remains unchanged. Missing/invalid enablement or non-development environment disables export without separate approval. Use safe correlation IDs/static stages, allowlisted diagnostics, sanitized errors, and startup replacement of default processors so no parallel unfiltered export exists. Export/redaction failure drops diagnostics without breaking chatbot execution.

Trace inspection uses OpenAI project/dashboard developer access; app admins do not automatically gain it. Employee/admin endpoints expose no trace content/links. `store=false` controls model-response storage separately from SDK traces. Temporary chats remain unsaved, but may produce permitted provider-side diagnostic metadata when export is enabled. No temporary transcript, customer details, or raw chat/tool values may appear in trace payloads. This explicitly refines the earlier blanket no-tracing wording; no external monitoring platform is approved.

### Validated persistence and correctness modules

Replace the permanent SDK-session-adapter proposal with authoritative application-owned ChatRepository. Load/filter/bound committed context explicitly; run without automatic SDK permanent writes or provider continuation. Validate response and policy before atomic completed-turn commit; handle duplicates, retries, interruption, version conflicts, and failed persistence. Temporary state remains in one process only.

MetricRegistry owns versioned business meaning, allowed sources/dates/provenance/timezone/filters/scopes, deterministic calculations, coverage, and authorization. ResponseBuilder owns authoritative facts and one text/visual dataset; reject unsupported numerical claims. `converted_leads` remains disabled pending business definition and scope approval. Central AuthorizationService combines RBAC/ABAC with policy-version invalidation and aggregate disclosure safeguards. Retrieved text never becomes trusted instructions.

### Identity, cleanup, evaluations, and planning authorization

Retain signed server-only NextAuth-to-FastAPI identity, verified provider/app-user binding, current backend grants, and key/expiry/session checks. Specify rotation, CSRF, login-session invalidation, logout/cancellation cleanup and failure bounds; do not promise immediate remote memory deletion if every notification fails. The earlier 30-minute idle expiry remains a proposed setting; exact leases/retention/disclosure thresholds stay deferred to existing technical documents.

Record reproducible synthetic/injected/isolated-DB evaluations for metric/tool/date correctness, evidence/visual agreement, access/revocation/injection, mutation prevention, authoritative commits, temporary privacy, and tracing exclusion/failure behavior. No tests or features were implemented in this step.

The user explicitly authorized updating Tickets.md now: TICKET-001 through TICKET-016 are Backlog architecture-foundation/verification tasks, an exception to previous later ticket-generation timing. No ticket is Ready/In Progress/Done. Existing downstream placeholders receive handoff references only; Tech Stack and full downstream drafts remain unstarted. No new services/infrastructure/source-schema/credential changes are introduced. No unresolved requirement contradiction remains after these explicit supersessions; technical values/layouts listed in Architecture section 11 still need specification.

## 2026-10-22 - Revised Architecture Approved; Tech Stack Drafted

The user approved the revised Architecture and authorized `05-TECH-STACK.md` only, with no coding. Architecture approval metadata is updated; Tech Stack remains Draft - Awaiting Review. Earlier Architecture review-status entries above are historical.

The Tech Stack draft recommends Node.js 24 LTS, Python 3.13, compatible candidate pins for the selected frameworks/SDKs, stable NextAuth V4 rather than V5 beta, PyMongo Async for the new backend, Recharts, standard token libraries, uv/npm lockfiles, and local quality/testing tools. These are proposed choices, not approved package selections or a tested installation. Runtime/package metadata and official guidance were checked; dependency resolution, SDK/exporter compatibility, and exact locks remain for authorized implementation. No model change, source-repo migration, application dependency install, manifest/lockfile creation, environment edit, ticket execution, or database operation occurred.

## 2026-10-22 - Tech Stack Approved; Database Schema Drafted

The user approved `05-TECH-STACK.md` and authorized drafting `06-DATABASE-SCHEMA.md` with reference to the three cloned projects and existing discovery. Tech Stack recommendations are approved, but candidate pins are not installed/resolution-tested. No coding or database changes are authorized by this step.

The Schema draft proposes nine AI-only collections: users, roles, access_requests (including preapprovals), identity_mappings, login_sessions, chat_sessions, chat_turns, audit_events, and app_settings. Source-qualified IDs preserve operational identifier types; chatbot users/permissions are separate from legacy accounts. Proposed indexes apply only to `byd_ai_layer`.

The proposed commit mechanism is a short app-database transaction: conditionally update a shared policy-control fence, insert a completed turn, and advance the owner/session version/context together. Access/revocation changes share the guard. Idempotency/version constraints, current-grant checks, retention/deletion, and memory-only temporary contents are specified; transaction support and numeric policy settings remain unverified. These schema choices await review, not implementation.

Reference review confirmed that the current `byd-leads-backend/models/Lead.js` already includes `dealer_studio`; Scope's contrary earlier enum statement was corrected explicitly. This is a factual correction, not a change to approved product scope. The prior live discovery remains bounded historical evidence; no new database connection, account/index/schema change, environment edit, feature code, or ticket execution occurred.

## 2026-10-22 - Database Schema Approved; API Contracts Drafted

The user approved `06-DATABASE-SCHEMA.md` and authorized `07-API-CONTRACTS.md` only. Schema approval metadata/headings now distinguish approved designs from uncreated collections/indexes; open policy values and transaction capability remain unverified. No implementation is authorized.

The API draft defines the Next.js/FastAPI boundary, server-only identity exchange/renewal/revocation, access administration, saved/temporary chat routes, separate audited history review, typed read tools, structured evidence/visual responses, safe errors, and atomic commit/retry recovery. Expected policy/resource versions prevent stale admin edits; no operational mutation or tracing-inspection endpoint is introduced.

Proposed review values include 120-second assertions, a five-minute login lease, 30-minute temporary idle expiry with a maximum 60-second cleanup sweep, bounded pages/messages/visuals, query/turn deadlines, model budgets, and admission limits. These API choices await review and are not approved security settings or measured guarantees. Security will finalize key/replay/disclosure details; unresolved business semantics/retention remain explicit. Existing architecture tickets remain Backlog; no new competing task list, next document, application code, database/configuration change, or dependency install is authorized by this draft.

## 2026-10-22 - API Contracts Approved; UI/UX Guidelines Drafted

The user approved API Contracts and requested UI library suggestions before drafting UI/UX Guidelines. Recommendations were presented first. API approval metadata now records the routes, shapes, and starting limits as approved designs; exact Security details remain open and the limits are not measured guarantees. No implementation is authorized.

The UI draft recommends owned shadcn/ui components, TanStack Table for advanced tables, react-markdown with optional remark-gfm for restricted prose formatting, and the approved Recharts with matching shadcn styling. TanStack Table is the table-behavior layer, not a Markdown renderer. React state/fetch and backend authorization remain unchanged; no additional agent SDK, Query/Router package, or service is proposed. Official library documentation was checked through Context7 and primary web sources.

These UI additions, the light neutral/blue styling, and responsive chat/admin layout await review. Visible notes in Tech Stack/API Contracts flag the proposed library and answer-presentation refinements; they are not silently approved changes. API answer shape and evidence/numeric checks are preserved. Saved/temporary disclosures, current-grant history review, safe recovery states, accessible visuals, and restricted Markdown are specified. Branding/dark mode remain open. No packages, UI code, database/configuration changes, new tickets, or browser/application tests were introduced.

## 2026-10-22 - UI/UX and Libraries Approved; Security Drafted

The user approved UI/UX Guidelines and the recommended libraries, then authorized only `09-SECURITY.md`. shadcn/ui, conditional TanStack Table, react-markdown/optional remark-gfm, Recharts composition, and restricted Markdown presentation are now approved recommendations. UI/Tech Stack/API approval notes are updated; no package compatibility, installation, or implementation is claimed. Branding/dark mode remain open.

The Security draft defines SEC-01 through SEC-15 for explicit identity/action/resource checks, RBAC/ABAC, policy-fenced changes/commits, source read restrictions, AI injection boundaries, browser/CSRF/transport defenses, conversation privacy, sanitized native tracing/audits, secrets/dependencies, incident response, and future verification. It preserves unchanged MongoDB credentials, two services/one agent, memory-only temporary chats, and the API's 120-second assertions/five-minute lease/30-minute idle/60-second sweep.

New proposals await Security approval: RS256 with 3072-bit RSA, 30-second clock tolerance, operation-bound assertions, an eight-hour absolute app-login lifetime, maximum 90-day signing-key age with 210-second planned verification overlap, immediate compromised-key invalidation, purpose-separated cursor/CSRF keys, and 64 KiB request/1 MiB response-document caps. These are design rules, not implemented controls or permission to provision/rotate keys. Exact retention, aggregate disclosure/differencing, admin MFA assurance, framework CSP/CSRF integration, and provider account/retention settings remain explicit review/verification items.

NIST/OWASP/JWT/NextAuth and current official OpenAI guidance plus SDK source via Context7 informed the draft. Broad source DB grants, local-development HTTP, host compromise, application-only audit immutability, and provider retention are acknowledged limits; no production certification or enterprise-wide zero-trust completion is claimed. No architecture redesign, extra service, environment edit, database operation, ticket execution, dependency installation, or application code is authorized by this documentation step.

## 2026-10-22 - Security Approved; Test Strategy Drafted

The user approved Security and authorized `10-TEST-STRATEGY.md` only. SEC-01 through SEC-15, including the crypto/rotation/absolute-login/byte-cap settings, are approved designs. Metadata/proposal labels and API/UI handoff notes are synchronized; implementation, credentials, keys, and database state remain untouched. Retention/disclosure/MFA/provider evidence and framework integration still require the stated confirmation/verification.

The Test Strategy draft separates offline unit/contract/UI checks, isolated Mongo integration, browser/manual journeys, and optional synthetic live-model/Google checks. It covers Q-01 through Q-05, numerical/evidence consistency, all security controls, source mutation prevention, atomic/idempotent saved turns, temporary non-persistence, sanitized tracing, bounded resources, and truthful ticket evidence. Real integrations are not claimed from mocks; no production writes or copied customer fixtures are allowed.

Proposed review targets: four phrasings per enabled question family with additional negative/follow-up cases; three repetitions for optional live-model variants; at least 95% correct intent/tool/arguments with 100% numerical/evidence consistency and security/privacy cases. Applicable ticket/static checks must pass; skipped/unavailable verification remains pending. Commands are future conventions, not created/runnable scripts. No new coverage plugin, evaluation platform, infrastructure, tickets, dependencies, or code is introduced. The Implementation Plan remains Not Started, and all 16 real tickets remain Backlog.

## 2026-10-22 - Test Strategy Approved; Implementation Plan Drafted

The user approved Test Strategy and authorized only `planning/IMPLEMENTATION-PLAN.md`. Test gates, evaluation targets, and future command conventions are now approved designs; no scripts or test results are claimed. Its approval metadata/footer and review labels are synchronized without changing those requirements.

The plan orders all 16 existing tickets while preserving their current dependencies: identity/policy, reads/normalization, metrics/disclosure/responses, saved turns, safe tracing setup, agent/temporary/stage tracing, and broader evaluations. It places TICKET-011/012 before TICKET-009 as a safety sequence, without changing registered dependency fields. Per-ticket tests begin immediately; final evaluation tickets do not postpone security verification. The plan awaits review and authorizes no execution.

The existing register explicitly covers architecture foundations, not all release work. The plan records gaps in local setup/test harness, app-data repositories/index ownership, complete API integration, and frontend/access-management journeys. These are coverage findings, not new tickets or a second task list. After plan approval, recommended authorized refinement will assign missing work unique ticket IDs and clarify bootstrap/storage prerequisites before affected implementation. No new ticket/status/dependency change was made here.

First delivery remains the approved last-seven-days lead question/card with saved follow-up context, then enabled Q-02–Q-05 and the remaining approved UI/admin/temporary journeys. Missing business/retention/disclosure/provider evidence limits only affected features or readiness claims. No new credentials, Atlas setup, services, deployment architecture, dependencies, keys, database operations, or application features were introduced. All 16 tickets remain Backlog; no coding, application tests, or browser verification ran.

## 2026-10-22 - Implementation Plan Approved; Tickets Refined

The user approved the Implementation Plan and explicitly authorized refining Tickets.md for missing setup, app-data, API and UI coverage, then stopping for review. Plan approval metadata and coverage/owner references are synchronized; approved milestones, scope and technical decisions remain intact. Test Strategy's handoff now points to ticket review.

The refined register retains TICKET-001–016 and adds TICKET-017–058: 42 new tickets, 58 total, all Backlog. Each actual ticket has a type, priority, milestone, requirement references, explicit dependencies, S/M complexity, bounded scope, three acceptance criteria, verification, notes and empty implementation evidence. No ticket is Ready or executed; the decomposition itself awaits user review.

Ownership changes make prerequisites explicit without dropping requirements. App storage/login metadata precede the identity bridge; bootstrap/decisions and grant services are TICKET-025/026, Google integration TICKET-031, saved history/deletion TICKET-029, and browser/admission security TICKET-056/057. TICKET-005 is narrowed to Q-01; TICKET-050–053 add Q-02–Q-05 after first-slice verification TICKET-054. TICKET-055 covers full enabled local acceptance; TICKET-058 owns verified local documentation/incident guidance. Safe trace setup/sanitization are explicit TICKET-009 dependencies.

The register's coverage index is navigation, not another task list; all authoritative status/dependency fields remain in Tickets.md. Setup candidates after review are TICKET-017/018, one only when explicitly authorized. Undefined business/retention/disclosure settings remain gated and mocks do not prove live integration. Existing credentials, source schemas/repos, approved libraries, two services/one agent, and local-only scope are preserved. No runtime code/tests, installations, manifests/locks, environment edits, keys, database changes, browser checks, provider spending or deployment occurred.

## TICKET-024 approval — 2026-10-23

User approved TICKET-024 with its recorded limits and replay-safe cleanup change.
The original-authentication timestamps and purge_after retention refinement in
Database Schema are approved. Real MongoDB TTL/index/race evidence remains
pending; approval does not assert those checks passed. TICKET-001 was authorized.
