# Feature & Development Ticket Register

Tickets.md is the single source of truth for project work. Do not create real feature tickets until the PRD and scope have been reviewed. Every planned implementation task must have a unique ticket ID and appear here.

Status: Approved Register - TICKET-001 Done  
Updated: 2026-10-23  
Authorization: User approved TICKET-001 with recorded limits, authorized GitHub push and then TICKET-027. TICKET-001 and TICKET-017–024 are Done; 49 tickets remain Backlog before TICKET-027 starts.

## Review and Execution Rules

- Approved documents 01–10 and the [Implementation Plan](planning/IMPLEMENTATION-PLAN.md) define scope. This register is the only authority for task IDs/status/dependencies; its refined decomposition is approved.
- Work one explicitly authorized Ready ticket at a time after dependencies are Done and relevant decisions are reviewed. Missing external verification stays pending; mocks never prove real integrations. Add tests with each ticket, not only in the final evaluation tickets.
- All source/business access stays application-enforced read-only; only app records may be written. Keep source repos/schemas/indexes/credentials and the two-service/one-agent design unchanged.
- New business/date/mapping/retention/disclosure values must not be invented. Implement/test safe unavailable/denied defaults and clearly labeled synthetic success policies; do not claim a gated feature is enabled. These gaps do not block unrelated offline work.
- Every ticket below includes implementation verification requirements, not results. Use approved Test Strategy/Security gates, safe evidence, and required review before Done. No operational mutation probes or live/provider spending without explicit authorization.

## Coverage Index

This is navigation within the register, not a second task list.

| Area | Ticket owners |
|---|---|
| Local packages/test harness | TICKET-017–020 |
| App connection/fence/audit and identity/access storage | TICKET-021–024, TICKET-027 |
| Bootstrap/access workflows | TICKET-025–026 |
| Saved parents/commit/history/deletion | TICKET-028, TICKET-008, TICKET-029 |
| API contracts/identity/access/chat/review | TICKET-030–039 |
| UI renderers/layout/access/chat/review | TICKET-040–049 |
| Additional Q-02–Q-05 cases | TICKET-050–053 |
| First slice/full local acceptance/runbook | TICKET-054–055, TICKET-058 |
| Web boundary/admission/cursors | TICKET-056–057 |
| Existing policy/read/metric/agent/tracing/evaluations | TICKET-001–007, TICKET-009–016 |

The first setup candidates after review are TICKET-017 and TICKET-018; select only one when explicitly authorized. Numeric ID order is not execution order: dependencies include later-numbered prerequisites. TICKET-054 defines the first slice; Q-02–Q-05 follow it, then TICKET-055 checks full enabled scope.

## Ticket Categories

- FEATURE: User-facing functionality
- BACKEND: Server-side implementation
- FRONTEND: Interface implementation
- DATABASE: Schema and persistence
- INTEGRATION: External APIs and services
- TEST: Automated and manual verification
- SECURITY: Security-related work
- DEVOPS: Deployment and infrastructure
- BUG: Defect correction

## Ticket Statuses

Backlog → Ready → In Progress → In Review → Done

Use Blocked when a task cannot proceed. Mark a ticket Done only after its acceptance criteria are met and completion evidence is recorded. Keep progress consistent with this register; do not maintain a competing task list.

## Ticket Template

### TICKET-NNN: [Ticket Title]

- Type:
- Priority: Critical / High / Medium / Low
- Status: Backlog
- Milestone:
- Requirement Reference:
- Dependencies:
- Estimated Complexity: S / M / L

#### Description

Explain exactly what needs to be implemented.

#### Implementation Scope

Describe the expected work and boundaries.

#### Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

#### Verification

Describe how completion will be tested and verified.

#### Implementation Notes

Relevant technical considerations.

#### Completion Evidence

Test results, files changed, or review references.

## Original Foundation Tickets

TICKET-001 through TICKET-016 retain their identities. The user's refinement request on 2026-10-22 narrows oversized ownership and adds explicit prerequisites. Bootstrap/access workflows now belong to TICKET-025/026, Google integration to TICKET-031, history/deletion to TICKET-029, browser security to TICKET-056, and additional question families to TICKET-050–053. Approved requirements are preserved through those owners, not dropped.

### TICKET-001: Verify the signed server identity bridge

- Type: SECURITY
- Priority: Critical
- Status: Done
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-AUTH; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-018, TICKET-019, TICKET-022, TICKET-024
- Estimated Complexity: M

#### Description

Implement the Next.js-to-FastAPI signed identity contract without trusting browser roles or emails.

#### Implementation Scope

Purpose-separated assertion signing/verification, operation binding, key IDs/rotation/revocation, and persisted login-session validation. Google callback/transport belongs to TICKET-031; bootstrap/approval workflows to TICKET-025; browser CSRF/proxy protections to TICKET-056.

#### Acceptance Criteria

- [x] Forged, expired, wrong-purpose/audience/operation, unknown-key, and invalid-session assertions are rejected using real validators and generated test keys.
- [x] RS256/3072-bit keys, 120-second maximum lifetime, 30-second tolerance, 90-day rotation and 210-second planned overlap follow Security; compromised keys have no grace.
- [ ] Internal tokens/keys never reach browser payloads/logs; exchange replay preserves its existing binding and cannot revive revoked sessions.

#### Verification

Use synthetic provider identities, injected signing keys/clock, server-route tests, and invalid/revoked session cases; no live Google/customer credentials in fixtures.

#### Implementation Notes

ARCH-AUTH; approved API/Security are authoritative. This ticket implements the bridge boundary, not access-management UI or bootstrap transactions.

#### Completion Evidence

Parent authored src/goodshowroom_ai/identity.py and frontend/src/lib/server/identity.ts; GPT-6 Luna max authored tests/test_identity_bridge.py only. Added exact runtime pins PyJWT[crypto] 2.15.1, jose 6.2.13 and server-only 0.0.1, with Python/frontend manifests and lockfiles. Updated README, Tickets, Progress, Schema approval note and Decisions. No real keys generated, .env edits, DB changes, Google calls, HTTP routes or browser features.

Signer uses the server-only marker and jose RS256, exactly 3072-bit RSA, fixed issuer/audience/kid, generated jti, bounded purpose-separated claims and 120-second lifetime. Verifier uses real PyJWT signature/issuer/audience validators with a fixed algorithm; manual strict integer/date checks use the injected clock for 120-second max lifetime/30-second assertion tolerance. Login expiry/lease/start have no skew. Header allowlisting denies token-directed keys/URLs; unknown claims/role grants, wrong purpose, overlarge tokens, wrong method/path/digest and malformed UUIDs deny with a fixed error. Raw operation digest is length-prefixed query bytes plus body SHA256; the normalized method/path are separately signed and compared.

Known-key metadata checks issuance within the 90-day activation window, 210-second planned retired-key overlap and immediate compromised-key denial. Registry state is checked again after the awaited loader; replaced/revoked key state denies. Request assertions require current loaded LoginRecord matching session ID, owner, generation, exact validity and revocation. Bootstrap checks verified Google profile/original auth timing; existing login bindings cannot revive after revocation/expiry. Identity-to-user resolution/approval remains TICKET-025. Recheck the live registry/session before protected release/commit; TICKET-002 and transport services must wire that check. No role or email is accepted as business authorization.

Verified: uv sync --locked passed; Ruff check and format passed (20 Python files); final default offline Pytest passed 151 cases in 35.84 seconds (103 existing + 48 new). Generated runtime keys exercise real Node-to-Python signing, default server-only import denial, signatures, all claim/operation/time/key/session boundaries and revocation after await. Frontend lint/typecheck passed, existing component unit passed one, production build passed. Fixed one TypeScript Uint8Array generic mismatch and corrected an initial formatting command's working directory before passing reruns. Initial dependency fetches were network-blocked; authorized network installation succeeded without peer/integrity bypass. Official PyJWT/jose APIs were checked and linked in README.

Production npm audit reports zero findings; install's full-tree audit retains 8 previously accepted dev findings (2 moderate/6 high). PyPI metadata lists zero advisories for the PyJWT pin, not a complete Python transitive audit. Existing clean-machine npm ci/tooling and real-Mongo limits remain.

The combined browser/privacy/replay criterion stays unchecked pending actual callback/transport and real persisted exchange evidence. No live Google flow, HTTP middleware/routes, real key provisioning/rotation, cross-service transport or coordinated persistent invalidation of affected login generations after key compromise was exercised. Key/session registry lifecycle, restart persistence and operational compromise response require integration; the in-memory key registry does not claim to revoke database sessions automatically. TICKET-031 owns callback/transport, TICKET-056 browser protections, TICKET-025 bootstrap and TICKET-024 durable login storage. Current tests prove validator/module boundaries, not a working Google login or production security certification. Await user review before Done.


### TICKET-002: Centralize RBAC and ABAC authorization with policy versions

- Type: SECURITY
- Priority: Critical
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-AUTH; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-001, TICKET-022, TICKET-027
- Estimated Complexity: M

#### Description

Build AuthorizationService as the authority for role capabilities and record attributes.

#### Implementation Scope

Approval/active role, allowed tools, sites, selected/assigned records, fields, chat ownership/review, current policy versions, and final-release invalidation; no new policy framework.

#### Acceptance Criteria

- [ ] Missing/unverifiable grants deny access; per-user scope cannot widen the active role.
- [ ] Every protected action/tool/history path uses current policy and rejects stale/revoked evidence before commit/release.
- [ ] Employee session isolation and admin saved-review access are separate from operational grants and developer trace access.

#### Verification

Table-driven synthetic policy tests plus a mid-run role/grant-change scenario; include joined-record/field checks.

#### Implementation Notes

ARCH-AUTH; SEC-01/04/05. Read typed app grants/settings through TICKET-022/027. This policy module does not create access workflows or UI.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-003: Add the application-enforced OperationalReadRepository

- Type: BACKEND
- Priority: Critical
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-READ; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-021
- Estimated Complexity: M

#### Description

Expose only approved operational reads while leaving MongoDB credentials unchanged.

#### Implementation Scope

Private driver handles, collection/read-method allowlist, trusted query builders, recursive operator/stage checks, authorized joins, deadlines/projections/result limits/stable pagination. App writes remain isolated.

#### Acceptance Criteria

- [ ] The public interface exposes no mutation, raw driver/client, generic command, or arbitrary pipeline execution capability.
- [ ] Insert/update/delete/replacement/upsert, $out/$merge, nested write stages, server-side JavaScript, and unapproved operators/collections are rejected before driver execution.
- [ ] Input/join scopes are authorized before aggregate reads; test spies record no operational write command and app writers target only byd_ai_layer.

#### Verification

Interface/command-spy tests, nested hostile pipeline fixtures, unauthorized joins, timeout/pagination fixtures, and isolated test-DB reads only.

#### Implementation Notes

ARCH-READ; SEC-06. Application protection, not credential protection; never probe production write denial.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-004: Normalize verified source dates, identifiers, sites, and staff scope

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-METRICS; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-003, TICKET-027
- Estimated Complexity: M

#### Description

Translate observed source variants into explicit authorized metric inputs without modifying operational documents.

#### Implementation Scope

Date types/provenance, source/status variants including dealer_studio, source-qualified IDs, explicit relationship links, and verified staff/site mappings.

#### Acceptance Criteria

- [ ] Missing/import-substituted dates are flagged; calendar/UTC/DST conversions do not silently change received-date meaning.
- [ ] Unknown ownership/site mapping cannot widen access; source names/phones are not treated as proof of identity.
- [ ] Mixed IDs, unmatched relationships, and live platform variants produce deterministic normalized evidence and coverage warnings.

#### Verification

Synthetic fixtures reproducing Scope's observed shapes, missing dates, sparse staff IDs, and unmatched delivery links; no copied customer values.

#### Implementation Notes

ARCH-METRICS; Scope section 5. Reviewed source/date/site/staff mappings only; unverified combinations stay unavailable. No source-schema/index/credential changes.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-005: Define MetricRegistry and the first lead-count metric

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-METRICS; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-004
- Estimated Complexity: M

#### Description

Centralize metric meaning and deterministic calculations, starting with the approved lead questions.

#### Implementation Scope

Versioned registry and total_leads for Q-01: reviewed sources, original-date provenance, calendar timezone/filters, deterministic full count, coverage and authorization. Q-02–Q-05 implementations move to TICKET-050 through TICKET-053.

#### Acceptance Criteria

- [ ] The Q-01 registry entry defines source identity/exclusions, date reliability, timezone, filters, aggregation, coverage and access; incomplete rules return partial/unsupported states.
- [ ] Previous seven completed dealership days exclude today; permitted full counts, boundaries/DST, verified zero and unknown dates have exact fixture results.
- [ ] converted_leads remains explicitly disabled; later question families cannot be enabled until their own reviewed rules and tickets are satisfied.

#### Verification

Exact expected counts on isolated fixtures with frozen time, missing dates, source variants, exclusions, and allowed scopes.

#### Implementation Notes

ARCH-METRICS; Q-01 is the first slice. Registry fixtures are not approval of real source provenance, exclusions, or conversion meaning.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-006: Enforce aggregate disclosure and small-group safeguards

- Type: SECURITY
- Priority: High
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-AUTH; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-005
- Estimated Complexity: M

#### Description

Prevent authorized aggregates from revealing information beyond the approved disclosure policy.

#### Implementation Scope

Metric-specific aggregate permissions, configurable minimum-group/suppression rules, complementary totals and repeated differencing controls, and safe broader-result fallback.

#### Acceptance Criteria

- [ ] Aggregation input is record-authorized before calculation; field/grouping permission is checked independently.
- [ ] Sensitive breakdowns without an approved disclosure policy are withheld; threshold values come from reviewed policy, not the model.
- [ ] Suppression cannot be bypassed using totals, complementary groups, repeated filters, exports, or visual datasets.

#### Verification

Synthetic threshold/complement/differencing cases and role matrices; assert text and visuals expose the same permitted aggregate.

#### Implementation Notes

ARCH-AUTH; SEC-05. Approved disclosure values are still open; test default withholding and clearly labeled synthetic policies. No arbitrary threshold or durable temporary query ledger.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-007: Build ResponseBuilder with evidence-backed facts and visuals

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-RESPONSE; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-005, TICKET-006
- Estimated Complexity: M

#### Description

Make verified evidence the source of every numerical fact and visualization.

#### Implementation Scope

Typed evidence/fact references, deterministic simple-metric templates, text-claim grounding, one visual dataset, provenance/coverage/freshness, Pydantic validation, and non-streaming output.

#### Acceptance Criteria

- [ ] Text, metrics, tables, and approved visual specifications use identical authorized facts with traceable metric/source references.
- [ ] Unsupported numerical claims are rejected/rebuilt deterministically; model self-checking alone cannot authorize facts.
- [ ] Verified zero, missing/partial data, source failure, denied access, and unsupported metric are distinct structured outcomes.

#### Verification

Fixture-based exact response/visual comparisons, injected unsupported model numbers, coverage failures, and safe-rendering payload checks.

#### Implementation Notes

ARCH-RESPONSE; SEC-07/09. Structured output only; rendering is TICKET-040. No downloadable reports or model HTML/JavaScript.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-008: Implement atomic authoritative saved-turn commits

- Type: DATABASE
- Priority: Critical
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-CHAT; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-007, TICKET-021, TICKET-028
- Estimated Complexity: M

#### Description

Make ChatRepository the authoritative permanent conversation store after validation.

#### Implementation Scope

App-only transaction/fence inserting one complete validated turn and advancing session state/summary/version with required audit; unique request/sequence indexes, minimal attempt status, and safe interruption/retry recovery. History/deletion is TICKET-029.

#### Acceptance Criteria

- [ ] Real isolated Mongo tests prove no partial turn/state/audit visibility; model calls and source reads stay outside the short transaction.
- [ ] Identical committed retries recover after current access checks; changed payloads/stale writers conflict; unknown commit outcomes reconcile without duplicate work.
- [ ] No raw SDK intermediate items persist; committed evidence/state retains owner, policy/metric versions, and size/retention linkage. Current access is checked before release.

#### Verification

Isolated app-test-DB atomicity/failure injection, duplicate/version conflicts, restart recovery, and revoked-history reads; operational DBs are never written.

#### Implementation Notes

ARCH-CHAT; approved Schema section 5 mandates the app-only transaction/fence. No silent separate-insert fallback; reads/deletion are owned by TICKET-029.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-009: Run the primary agent with explicit bounded authorized context

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-CHAT, ARCH-INJECTION; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-003, TICKET-005, TICKET-007, TICKET-008, TICKET-011, TICKET-012, TICKET-057
- Estimated Complexity: M

#### Description

Prepare model input in application code and keep working items ephemeral until final validation.

#### Implementation Scope

One primary agent, typed-tool selection, committed authorized history, recent turns/summaries, validated follow-up state, prompt/data boundaries, final policy checks, and save/memory dispatch.

#### Acceptance Criteria

- [ ] Runner receives explicit bounded history without a permanent SDK session or provider-managed continuation; no incompatible replay chain is used.
- [ ] Intermediate tool/model items remain ephemeral; only ResponseBuilder-validated content reaches the commit boundary.
- [ ] Follow-ups preserve permitted filters while refreshing data; stored notes/history cannot change tool availability, instructions, or scope.

#### Verification

Mock-runner call assertions, complete tool-pair/context fixtures, summary failure, hostile stored text, and mid-run permission-change tests.

#### Implementation Notes

ARCH-CHAT/INJECTION; SEC-07/10. Sanitized tracing prerequisites are explicit; export stays disabled unless approved settings and all safety checks pass. No SDK automatic permanent history.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-010: Enforce temporary-chat lifetime and coordinated logout cleanup

- Type: SECURITY
- Priority: Critical
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-TEMP; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-001, TICKET-002, TICKET-008, TICKET-009, TICKET-024
- Estimated Complexity: M

#### Description

Keep temporary conversation contents in process memory and prevent reuse after termination.

#### Implementation Scope

User/login-session-bound opaque IDs, memory-only context/locks/retries, explicit end, NextAuth logout/backend invalidation, cancellation, expiry/idle cleanup, restart loss, and failed-notification bounds.

#### Acceptance Criteria

- [ ] Temporary messages/state/summaries/tool evidence/visuals never reach permanent chat storage, browser persistent storage, ordinary logs, or trace content.
- [ ] End/logout/expired or revoked session/restart prevents context reuse and late results cannot resurrect it.
- [ ] Failed cleanup notifications have a specified bounded lease/idle fallback and accurate privacy wording; no silent durable recovery or extra worker/store is added.

#### Verification

Injected clock/cancellation/session faults, sign-out failure, abandoned chats, restart and late-response tests, plus persistent-storage sentinel scans.

#### Implementation Notes

ARCH-TEMP; SEC-11. One process; approved 30-minute idle expiry, five-minute login lease, 60-second sweep and eight-hour absolute login. Routes/UI are TICKET-039/048.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-011: Configure explicit developer-only native tracing

- Type: INTEGRATION
- Priority: High
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-TRACE; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-017, TICKET-019
- Estimated Complexity: M

#### Description

Map app tracing settings explicitly to current Agents SDK controls without enabling export by default.

#### Implementation Scope

APP_ENV/OPENAI_TRACING_ENABLED/OPENAI_TRACING_INCLUDE_SENSITIVE_DATA parsing, global/per-run/root controls, static workflow names, safe diagnostic IDs, and non-development denial.

#### Acceptance Criteria

- [ ] Missing/invalid enablement or missing/non-development APP_ENV disables export; explicit development enablement permits metadata tracing.
- [ ] Real operational and temporary runs use trace_include_sensitive_data=False; any future synthetic content capture requires separate approval.
- [ ] SDK tracing_disabled/global/root controls agree before the first trace; store=False remains independent and no inspection route is exposed to app users/admins.

#### Verification

Configuration matrix with an injected exporter and no provider traffic; verify no span/export creation while disabled and no credential values in settings logs.

#### Implementation Notes

ARCH-TRACE; SEC-12. App flags are not SDK-native variables. Capture-only test exporter; no real export until TICKET-012 and provider access/retention review.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-012: Sanitize SDK payloads before native OpenAI trace export

- Type: SECURITY
- Priority: Critical
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-TRACE; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-011
- Estimated Complexity: M

#### Description

Eliminate sensitive-data and parallel-unfiltered export paths while retaining native SDK tracing.

#### Implementation Scope

Startup processor replacement, compatible BatchTraceProcessor/sanitizing exporter/BackendSpanExporter composition, payload-copy allowlists, standard/custom error sanitization, bounded nonfatal export.

#### Acceptance Criteria

- [ ] set_trace_processors replaces the default path before any trace; adding an independent redaction observer is not accepted.
- [ ] Only approved static names, opaque correlation IDs, outcome/error categories, durations/usage, and schema-compatible SDK fields are exported; secret/PII/chat/business-value sentinels are absent.
- [ ] Serialization/redaction/export/flush failures drop diagnostics with fixed safe logs; no raw fallback or chat-execution failure occurs.

#### Verification

Capture serialized payloads at the exporter boundary using injected destinations; test all unsafe sentinel types, malformed payloads, worker/flush/shutdown failures, and exporter compatibility against the pinned SDK.

#### Implementation Notes

ARCH-TRACE; SEC-12. Native exporter only; no additional platform/destination. Serialized allowlist must satisfy approved Security, including unsafe standard SDK error/metadata fields.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-013: Instrument request stages and document developer trace access

- Type: BACKEND
- Priority: Medium
- Status: Backlog
- Milestone: Architecture foundations
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-TRACE, ARCH-TEMP; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-009, TICKET-010, TICKET-012
- Estimated Complexity: M

#### Description

Expose useful sanitized diagnostic stages across the full agent request.

#### Implementation Scope

One outer native trace with authorization/context/validation/tool/data/response/final-policy/persistence spans, available latency/usage/outcomes, and developer-project access/privacy documentation.

#### Acceptance Criteria

- [ ] Saved and temporary workflow names/spans show where validation, authorization, source/model/response/persistence failures occur without raw arguments or records.
- [ ] Only authorized OpenAI-project developers inspect traces; employee/admin API/UI responses include no trace details/links or inspection endpoint.
- [ ] Temporary metadata-versus-transcript and store=False-versus-export distinctions are disclosed; trace failures cannot alter permission or persistence outcomes.

#### Verification

Injected trace sink asserts stage nesting, safe metadata, denied/failed/success paths, and application access-route exclusions; record a project-access review before real export.

#### Implementation Notes

ARCH-TRACE/TEMP; SEC-12/13. Real project access/retention must be reviewed before export; unavailable metadata is not fabricated.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-014: Create reproducible metric and agent-quality evaluations

- Type: TEST
- Priority: High
- Status: Backlog
- Milestone: Architecture verification
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-EVAL; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-005, TICKET-007, TICKET-009, TICKET-019
- Estimated Complexity: M

#### Description

Provide representative synthetic business questions with exact expected tools, metrics, coverage, and visuals.

#### Implementation Scope

Reusable versioned corpus/scoring harness with Q-01 cases, frozen clock/grants, expected tools/arguments/facts/coverage, exact visual consistency, and follow-up/negative cases. TICKET-050–053 add their question-family cases.

#### Acceptance Criteria

- [ ] The suite reproduces expected initial lead answers and text/chart consistency without a live model or production database.
- [ ] Wrong tool/filters, unsupported numbers, missing coverage, and zero-versus-failure confusion produce visible test failures.
- [ ] Optional live-model evaluations record model/prompt/metric versions and safe results separately; no external platform is required.

#### Verification

Run the documented local fixture evaluation command and preserve actual results at implementation time; fixtures contain no real customer records.

#### Implementation Notes

ARCH-EVAL; approved Test Strategy owns commands/thresholds. Each enabled family ultimately needs four phrasings; optional synthetic live variants repeat three times. No real model score is claimed from mocks.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-015: Evaluate authorization, injection, and operational mutation boundaries

- Type: TEST
- Priority: Critical
- Status: Backlog
- Milestone: Architecture verification
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-EVAL, ARCH-AUTH, ARCH-INJECTION, ARCH-READ; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-003, TICKET-006, TICKET-009, TICKET-056, TICKET-057
- Estimated Complexity: M

#### Description

Verify that hostile prompts/records cannot escape trusted access and query boundaries.

#### Implementation Scope

Forged/expired principals, cross-user/record/site/field/collection/join attacks, aggregate disclosure, policy changes during execution, prompt injection in notes, and repository command/interface checks.

#### Acceptance Criteria

- [ ] Stored/user instructions cannot change tools, permissions, database scope, or trusted instructions; denied fields/records never reach the model or answer.
- [ ] Mid-run policy changes and small-group inference attempts fail according to reviewed policy.
- [ ] Operational read paths expose no mutation/escape capability and spies show no unauthorized/write command; no production mutation probe is used.

#### Verification

Adversarial isolated fixtures and injected model/policy/driver faults; preserve reproducible failing/passing case evidence only when actually run.

#### Implementation Notes

ARCH-EVAL/AUTH/INJECTION/READ; SEC-01–09/14. Exercise real security validators, including route/CSRF/proxy helpers; new endpoint tickets add their own route attacks.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-016: Verify conversation atomicity, temporary privacy, and trace exclusions

- Type: TEST
- Priority: Critical
- Status: Backlog
- Milestone: Architecture verification
- Requirement Reference: [Architecture](docs/04-ARCHITECTURE.md), ARCH-EVAL, ARCH-CHAT, ARCH-TEMP, ARCH-TRACE; [PRD](docs/01-PRD.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-008, TICKET-010, TICKET-013, TICKET-029
- Estimated Complexity: M

#### Description

Run cross-boundary failure tests for chat commits, cleanup, and exported diagnostics.

#### Implementation Scope

Atomic/duplicate/interrupted turn cases, owner/admin isolation, stale history, logout/lease/restart/late responses, tracing configuration/redaction/export faults, and temporary storage sentinels.

#### Acceptance Criteria

- [ ] No partial/duplicate authoritative turn, stale-policy release, cross-user recovery, or temporary persistent transcript appears under tested failures.
- [ ] Temporary content and secret/PII sentinels are absent from captured logs/exported traces/persistent stores; metadata-only traces follow explicit configuration.
- [ ] Export/redaction/flush failures remain nonfatal while real access or validation failures still deny output; privacy notices match tested lifecycle limits.

#### Verification

Isolated app DB, in-memory fake model/clock/session controls, captured trace destination, and fault-injection tests; record actual commands and outputs later.

#### Implementation Notes

ARCH-EVAL/CHAT/TEMP/TRACE; SEC-10–13. Privacy captures remain in memory; no persisted temporary screenshots/traces/snapshots. New route/UI tickets add their own checks.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

## Added Setup, App-Data, API and UI Tickets

### TICKET-017: Initialize the local FastAPI package

- Type: DEVOPS
- Priority: High
- Status: Done
- Milestone: Local setup
- Requirement Reference: [Tech Stack](docs/05-TECH-STACK.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: None
- Estimated Complexity: S

#### Description

Create only the approved Python package and local run/check entry points.

#### Implementation Scope

Project manifest/lock, module layout, environment loading, one-process development entry and non-sensitive startup output; no business routes.

#### Acceptance Criteria

- [x] Approved dependency candidates resolve and lock without peer bypass; deviations are documented for review.
- [x] Startup can run with injected offline settings; protected operations remain unavailable until their security modules exist.
- [x] Existing .env values/source/test structures are preserved; README gains accurate backend run and static-check instructions.

#### Verification

Package/import and local startup smoke, Ruff checks, missing-config and secret-output checks.

#### Implementation Notes

Local setup only; no CI, deployment, containers, live DB connections or key provisioning.

#### Completion Evidence

2026-10-23: Added pyproject.toml, uv.lock and src/goodshowroom_ai package; updated README and ignore rules. Python 3.13.5 / uv 0.10.4 resolved and installed 18 packages. Ruff check and format checks passed. Offline factory/configuration and local startup smoke checks recorded in PROGRESS.md. Initial smoke command had a quoting error and was replaced. No database/model connections. Only setup web/config dependencies installed; AI/data/identity/test harness dependencies remain with their owners. Security review: no protected routes, docs disabled, loopback single process, access logs off, sanitized config failure. User approved TICKET-017 on 2026-10-23; acceptance evidence reviewed and ticket marked Done.

### TICKET-018: Initialize the local Next.js package

- Type: DEVOPS
- Priority: High
- Status: Done
- Milestone: Local setup
- Requirement Reference: [Tech Stack](docs/05-TECH-STACK.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: None
- Estimated Complexity: S

#### Description

Create the approved frontend package and a minimal local shell.

#### Implementation Scope

Next.js/React/TypeScript/Tailwind configuration, npm lock and package scripts; no login or chat functionality.

#### Acceptance Criteria

- [x] Compatible approved packages lock; TypeScript/lint checks and local build/start smoke succeed.
- [x] Server-only configuration loads intentionally from the existing environment location; no private values enter client bundles.
- [x] README records actual frontend run/check commands; no operational backend or extra service becomes a dependency.

#### Verification

Build/type/lint smoke and client-bundle/environment sentinel inspection.

#### Implementation Notes

Add component sources only when needed; no speculative state/router or AI SDK packages.

#### Completion Evidence

2026-10-23, repository frontend/; Node 24.20.0 / npm 11.19.0. Added pinned Next.js 16.4.2, React/DOM 19.3.1, TypeScript 5.9.3, Tailwind/PostCSS 4.3.3; matching Next lint config and exact type-tool pins. Added package/lock, strict peer/exact/no-lifecycle-script policy, minimal App Router shell, server-only root environment loader, setup verification script, README run/check instructions and ignore rules. AGENTS.md reporting rules updated under separate explicit user authorization.

Verified: npm install --package-lock-only (normal resolution and final offline validation), npm rebuild --ignore-scripts --offline, npm ls --all --json (exit 0, no dependency problems), npm run lint (final exit 0, zero warnings), npm run check:setup (production build and HTTP startup, private marker/configured raw-value exclusion in static assets and HTML, unavailable API/.env paths 404, root environment loading and unchanged .env), npm run typecheck (exit 0, including after Next development type generation); separate loopback development-server HTTP smoke passed. Original .env hash also unchanged. No browser automation, database/model calls or operational mutations. Existing Python/source/test structures preserved. Working tree has no commit yet; evidence is for current local files, not a committed revision.

Installation limitation: repeated npm installs stalled on the Next archive/cache. A direct npm-registry archive matched the lockfile SHA-512 before extraction into generated node_modules; npm rebuild repaired executable links. The complete installed dependency tree validates, but an uninterrupted fresh npm ci is not proven. Partial dependency directory was removed after its absolute workspace path was verified. No integrity or peer bypass flags used.

Tooling review items: ESLint 10.13.0 was rejected because Next's import/React/accessibility plugin peers omit 10. Compatible ESLint 9.39.5 is end-of-life. Full npm audit reports five high entries from one development-only braces chain (GHSA-vfj7-8cjw-p6xm; no published patch); production-only audit reports zero. No forced downgrade/override applied. Current lint patterns are trusted local inputs and no app/API path invokes these tools. These limits require user review before Done. Initial lint warning in PostCSS config was fixed and lint rerun successfully; early checks before install could not find executables and are not counted as passes.

Security review: no login/chat/admin/API implementation; page renders static trusted text only. Configuration remains server-side, private values are not exported via Next env/public variables, default powered-by header is disabled, dependency scripts are disabled, and local run binds loopback. Full authenticated web protections remain TICKET-056; this shell is not a production security certification.

User review: Approved with the recorded installation/tooling limits on 2026-10-23. Marked Done. No claim of a clean npm ci or resolved development advisories is added.

### TICKET-019: Create the isolated backend test harness

- Type: TEST
- Priority: Critical
- Status: Done
- Milestone: Local setup
- Requirement Reference: [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-017
- Estimated Complexity: M

#### Description

Make ordinary backend tests deterministic and offline by default.

#### Implementation Scope

Injected model/policy/repository/exporter/clock fixtures, generated runtime keys, strict markers, network denial and verified test-DB namespace guards.

#### Acceptance Criteria

- [x] Default tests never load real .env or reach Google/OpenAI/Atlas; missing opt-in/unsafe DB targets fail safely.
- [x] Clock/race controls and lifespan/override cleanup isolate repeated tests; no committed keys, credentials or passwords.
- [x] Approved offline/integration/live command selections work; sample allowed/denied harness tests report actual results.

#### Verification

Run harness self-checks, blocked-network/target cases and fixture-leak checks.

#### Implementation Notes

No new DB is provisioned; real Mongo evidence remains pending until a verified test target is available.

#### Completion Evidence

2026-10-23; repo root; Python 3.13.5, uv 0.10.4. TICKET-017 prerequisite Done. Added tests/__init__.py, tests/harness.py, tests/conftest.py, tests/test_harness.py; updated pyproject.toml/uv.lock, .gitignore, README.md and progress. Exact development pins: pytest 9.1.1, pytest-asyncio 1.4.0, HTTPX 0.28.1, cryptography 50.0.2. Runtime packages/source modules unchanged. No commit exists; evidence describes current local files.

Checks: uv sync --locked succeeded; uv run --locked ruff check . passed; uv run --locked ruff format --check . passed (8 Python files). Explicit offline selector: 31 passed, zero failed/skipped/warnings. Default pytest selection also passed 31 cases. Integration/live selectors intentionally select no real cases yet (pending evidence, not passes); separate subprocess probes verify missing opt-in and unconfigured-adapter failures before test bodies, unknown marker rejection and collection-time network blocking. Session-exit probe verifies restoration of file/socket/settings guards and a scrubbed environment marker. Repeated fixtures verify fresh mocks/clock/environment and app lifespan/override cleanup on interruption. Keys are generated in memory, never serialized.

Early runs exposed Windows asyncio socketpair blocking and pytest traversal above temporary child-test roots. Fixed with a thread-local loopback exception only inside the synchronous standard-library socketpair call, and explicit child root/confcutdir. Final runs pass without suppressed warnings or skipped failures. Ordinary socket/DNS/UDP calls still deny. Environment-file opens (including write/read-write modes) deny; default Settings does not load .env. Test environment is restored per case and session. This is a Python regression guard for reviewed tests, not a hostile-code/OS sandbox; trusted plugins load before configuration hooks.

Database guard unit checks require a reviewed literal loopback endpoint, exact run namespaces and matching read-only ownership evidence; source/app names, suffix-only namespaces, mismatched endpoint/marker and marker-read failures deny. Synthetic readers prove guard logic only. No DB/model/provider adapter or connection, DB provisioning, source mutation or application feature was added. Both external opt-in flags remain fail-closed until the relevant reviewed adapters exist; no credentials are needed for ordinary work. No actual JWT/authorization/Mongo/provider assurance is claimed.

Official PyPI metadata for the four new direct pins reported zero listed advisory entries; this is not a complete transitive security audit. Existing frontend limits are preserved and accepted under TICKET-018. Security review: no secrets/credentials in fixtures; provider environment removed; tracing flags disabled; raw Python networking denied before test collection, with only Windows internal socketpair allowed. Required real validators will be tested by their future tickets, not replaced by permissive harness policies.

Separately authorized skill installation: official Vercel React Best Practices and Web Design Guidelines installed into C:/Users/Abrar/.codex/skills via the Codex skill-installer helper. Files/frontmatter verified. No frontend or AGENTS.md changes were needed. Await user review before Done.

User review: Approved on 2026-10-23. Marked Done with real integration/provider verification still pending as documented.

### TICKET-020: Create the frontend test harness

- Type: TEST
- Priority: High
- Status: Done
- Milestone: Local setup
- Requirement Reference: [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-018
- Estimated Complexity: M

#### Description

Provide reproducible component and synthetic browser-test entry points.

#### Implementation Scope

Vitest/Testing Library setup, isolated browser contexts and Playwright runner configuration; real journeys belong to TICKET-054/055.

#### Acceptance Criteria

- [x] Single-run component, lint and type-check scripts execute with synthetic API/identity responses.
- [x] Browser harness reaches only allowed local test surfaces; Google/provider calls are mocked by default.
- [x] Temporary/privacy scenarios disable content-bearing screenshots/videos/traces/snapshots and emit safe outcomes only.

#### Verification

Harness self-checks and local synthetic smoke; follow AGENTS.md for any authorized interactive browser verification.

#### Implementation Notes

Synthetic harness verification only. Real login/chat/admin and temporary-chat application journeys belong to TICKET-054/055 and their feature tickets. These checks do not prove those features.

#### Completion Evidence

2026-10-23; TICKET-018 prerequisite Done. Test-code authoring and fixes used GPT-6 Luna, max reasoning. Browser execution used GPT-6 Luna, medium reasoning. Updated frontend/package.json and package-lock.json. Added vitest.config.ts, playwright.config.mjs, tests/setup.ts, src/app/page.test.tsx; scripts/privacy-guard.mjs, test-network-policy.mjs, e2e-test-server.mjs, e2e-global-setup.mjs, test-environment.mjs, run-vitest.mjs, run-playwright.mjs, safe-vitest-reporter.mjs, check-test-harness.mjs; e2e/fixtures.mjs, synthetic-harness.spec.mjs, safe-reporter.cjs, mcp-synthetic-smoke.mjs. Updated root README.md, Tickets.md and planning/PROGRESS.md. No application feature or sibling repository was changed. The existing frontend/AGENTS.md was preserved.

Exact test-tool pins: @playwright/test 1.63.0, vitest 5.0.0, vite 8.2.2, @vitejs/plugin-react 6.1.1, @testing-library/react 16.3.3, @testing-library/dom 10.4.2, jsdom 30.1.3. Strict installation/reification and npm ls --all passed without peer/integrity bypass. Initial Windows dependency replacement failed because the active Next dev server held the Lightning CSS DLL. Stopped only identified project processes, repeated installation successfully, then restored Next dev on 127.0.0.1:3000. HTTP smoke returned 200 with the existing setup title and notice. No fresh clean-machine npm ci claim is added.

Final frontend commands: npm run test:unit passed one component test; npm run test:harness passed guard/reporter self-checks; npm run lint and npm run typecheck passed; npm run test:e2e -- --list discovered six cases. The delegated terminal npm run test:e2e passed all six cases: synthetic API/identity, external-request denial, redirect denial, direct API-context denial, WebSocket denial, and context isolation. No content-bearing test artifact directories were found after that run. Playwright MCP failed before helper execution with Target.createBrowserContext: Not allowed. It is not a passed MCP check; no shared-context retry was used. The MCP helper remains unverified.

Launchers use an OS/cache environment allowlist, remove inherited provider settings/Node options, preload guards, and reject unsafe CLI overrides. Vitest envDir is disabled. Node guards deny external fetch/TCP/DNS; only the fixed local socket endpoint is allowed. Browser fixtures permit exact local GET paths, deny redirects/direct API request contexts/WebSockets, block service workers, and close isolated contexts. The test-only server serves fixed invented data, checks Host, exposes no files, and loads no environment file. No real Google/OpenAI/Mongo/FastAPI service is used.

Privacy controls disable screenshots/videos/traces and snapshot/copy prompts, suppress raw stdout/stderr/error bodies through fixed reporters, and prevent the pinned Playwright error-context.md write through a preload. Self-checks simulate failure callbacks with a synthetic private sentinel, verify no sentinel in output and no diagnostic file, and test network/override denial. They do not prove an actual framework failure/artifact integration path. These are regression controls for reviewed tests, not an OS sandbox or a guarantee against native/untrusted code.

Initial browser execution left a shell-launched test-server process tree. Stopped only identified harness processes. Replaced Playwright webServer shell spawning with a runner-owned server in global setup and returned teardown that closes connections/listener. After this fix, independently repeated unit/harness/lint/typecheck successfully. The medium browser agent repeated all six cases: actual process exit 0, discovered=6, passed=6, failed=0, skipped=0. Port 4178 was free before/after, observed run-owned Node processes exited, and no matching test artifact directories were present. The developer server on port 3000 remained running.

Security review: full npm audit returned exit 1 with 8 development-tree findings (2 moderate, 6 high, zero critical); npm audit --omit=dev returned zero findings. Five high entries remain the previously accepted lint/braces chain. Newly added entries concern @vitest/mocker/vitest (moderate) and Vite (high). Npm's suggested patched pins were unavailable in the checked registry; alternative published older combinations had worse advisories and were discarded. Fixed one-shot execution, no exposed runner UI, synthetic data and network guards mitigate exposure but do not fix dependencies. The user approved TICKET-020 with these unresolved dependency-audit limits on 2026-10-23. No forced overrides, credentials, database provisioning or production readiness claim.

Baseline comparison confirmed unchanged backend manifest/lock/source. The ignored .env changed concurrently during this task; this task did not write it, and the current user values were preserved. No secret values were copied into evidence. The remaining audit findings and clean-machine installation limit remain documented risks under the user's approval.

### TICKET-021: Add app-only database and transaction primitives

- Type: DATABASE
- Priority: Critical
- Status: Done
- Milestone: App-data foundation
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-017, TICKET-019
- Estimated Complexity: M

#### Description

Create the private database foundation for approved application writes.

#### Implementation Scope

App connection lifecycle, fixed write target, common UUID/date/byte validation, access_control fence, audit repository and their app-only indexes.

#### Acceptance Criteria

- [x] App writers cannot select operational databases from inputs; startup/index routines never touch source collections/indexes.
- [ ] Shared transaction/fence and required audit writes are atomic; audit failure aborts protected changes.
- [ ] Common storage bounds and safe logs are tested; real isolated Mongo proves transactions/indexes before those capabilities are claimed.

#### Verification

Private-interface/target spies, isolated transaction rollback/conflict and audit-failure tests.

#### Implementation Notes

No source startup routines, destructive index repair, new server or credential changes.

#### Completion Evidence

Implemented app-only storage types and lifecycle in `src/goodshowroom_ai/storage/models.py`, `src/goodshowroom_ai/storage/mongo.py`, and `src/goodshowroom_ai/storage/__init__.py`. `AI_LAYER_MONGO_URI` is the only configured URI field. The client selects fixed database `byd_ai_layer`; connection is explicit and opt-in from the app lifecycle. Index setup is explicit and creates only the two `audit_events` indexes owned by this ticket. Imports and default startup do not connect or create indexes.

The typed internal context denies by default and grants only the fence/audit operations. The access-control fence update and required audit insert use the same session. Each transaction allows one fence update followed by its required audit event. A failed write poisons the transaction even if its error is caught by its caller; the handle expires when the transaction context exits. Audit records have schema version 1, fixed action/outcome/error categories, UUIDs, and UTC dates. BSON validation rejects documents over 1 MiB, invalid limits, non-finite values, unsupported BSON values, naive dates, cycles, and excessive nesting. Driver failures use fixed messages without raw errors.

`pyproject.toml` and `uv.lock` pin PyMongo 4.19.0. Installed API inspection confirmed `AsyncMongoClient.start_session()` returns an async session with `.bind()` and async context support. The suite uses injected fakes only. No `.env` file or secret value was read. No real MongoDB endpoint was contacted, and no collections or indexes were created or changed. Mock assertions and same-session spies do not prove server rollback or transaction/index support. Real isolated Mongo verification remains pending, so the atomicity and real-evidence criteria stay unchecked. No AuthorizationService, source read/write path, source schema/index, credential, frontend, or real database was changed.

Verified commands and results: `uv sync --locked` (exit 0; pinned PyMongo installed); `uv run --locked ruff check .` (exit 0); `uv run --locked ruff format --check .` (exit 0); `uv run --locked pytest` (56 passed, no skips or warnings). The first run exposed one test-order/assertion mismatch; it was corrected before the passing run. The focused storage suite has 25 tests; the original 31 harness tests remain passing.

Next eligible ticket after TICKET-021 is reviewed and marked Done: TICKET-022, which depends on TICKET-021. Do not start it before explicit authorization. The PRD release is not complete.

User review: TICKET-021 approved with its recorded limits. Marked Done. Real isolated Mongo transaction/index evidence remains pending; this approval does not verify it.

### TICKET-022: Persist chatbot users and roles

- Type: DATABASE
- Priority: Critical
- Status: Done
- Milestone: App-data foundation
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-021
- Estimated Complexity: M

#### Description

Store app identities and typed roles independently of legacy accounts.

#### Implementation Scope

Users/roles models, repository methods, unique provider/email/name indexes and resource versions; no access-management workflows.

#### Acceptance Criteria

- [x] Stable provider binding, email-collision rejection and strict grant/scope shapes match Schema.
- [x] Only typed identity/policy service contexts use these private repositories; no generic public CRUD/raw driver escapes.
- [ ] Uniqueness/version/conflict and document bounds have isolated tests; source users/passwords are never imported.

#### Verification

Repository authorization-context tests and real isolated uniqueness/version checks.

#### Implementation Notes

Do not infer roles or ownership from display email or source-system passwords.

#### Completion Evidence

Application code authored by the parent agent; tests authored only by GPT-6 Luna max as requested. Added storage/identities.py, updated storage/models.py and storage/mongo.py, added tests/test_identity_storage.py, and updated README/Tickets/PROGRESS. No dependencies or lockfiles changed.

User and role records have strict bounded fields, UUID IDs, schema/version metadata and UTC dates. Grants use typed dataset/site/record-mode/field/metric/tool selectors; selected source references retain tagged key types. Pending-only creation grants no access. Profile edits preserve immutable provider binding and existing access. Named read/write methods require explicit internal capabilities; future identity/policy services must supply verified grants and enforce active-role/scope intersections. No generic public CRUD or raw collection interface. Role/profile updates use expected-version CAS; each identity-writing transaction increments policy_version once after the shared fence and requires its audit. DuplicateKeyError becomes a safe conflict, with transaction poisoning. App-only unique-index definitions cover provider identity, email and role name. Setup is explicit; no actual index was created.

Final commands: uv run --locked ruff check . passed; uv run --locked ruff format --check . passed (14 files); uv run --locked pytest --strict-markers -m "not integration and not live" passed 69 cases in 21.81 seconds, including all existing 56 and 13 new identity tests. First overlapping parent run saw two stale test assertions while the test file was being revised; final settled-file rerun passed. Tests use fakes for uniqueness/session/CAS conflicts, policy increments, denial, round-trips and bounds. Real isolated Mongo unique-index/concurrent-version/rollback evidence is pending, not a pass; the corresponding combined criterion remains unchecked. No verified isolated test target was used, and no database provisioning or live writes were attempted.

Security review: no source users/passwords or secrets imported; fixed app target retained; unsafe selectors/extra fields denied; email collisions never rebind accounts; profile access edits denied; stale replacements and duplicate keys conflict; storage size checked on reads/writes; driver errors remain fixed messages. No HTTP/login/admin workflow or complete AuthorizationService is provided by this storage ticket. Existing accepted dependency/real-Mongo limits remain. No .env, frontend, sibling repository or operational database/schema/index/credential edits. Await user review before Done.


User-reported Windows verification fix (TICKET-022 follow-up): reproduced the original command under the human Abrar account: cacheprovider failed on sandbox-owned .pytest_cache; with cacheprovider disabled, 62 passed and seven tmp_path setups failed on shared pytest-of-unknown. Retaining user names alone exposed the same pre-existing ownership problem on pytest-of-Abrar. Fixed the harness with default -p no:cacheprovider and a fresh process-owned TemporaryDirectory as default basetemp; cleanup targets only that generated tree. Preserve OS user-name environment settings; retain strict warnings, network/env-file denial and marker gates. No old/shared folders deleted or permissions widened. Explicit --basetemp behavior is unchanged.

GPT-6 Luna max authored the harness changes and regression tests. Final normal-user verification: uv run --locked ruff check . passed; uv run --locked ruff format --check . passed (14 files); uv run --locked pytest passed 71 tests in 25.37 seconds, exit 0. Two new regression cases plus an extended restoration test cover cache disabling, writable per-run temporary storage and user-name preservation/restoration. README now recommends the short default offline command on its own line; the original pasted command also accidentally appended cd to pytest. No application feature or database change. TICKET-022 remains In Review; do not start TICKET-023.
User review: TICKET-022 approved with its recorded limits and marked Done. Real isolated Mongo evidence remains pending.

### TICKET-023: Persist access requests and email preapprovals

- Type: DATABASE
- Priority: High
- Status: Done
- Milestone: App-data foundation
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-022
- Estimated Complexity: S

#### Description

Store the approved access-request state machine.

#### Implementation Scope

Combined request/preapproval records, active partial-unique index, allowed status transitions and bounded admin-list queries.

#### Acceptance Criteria

- [ ] Repeated pending requests return one active row; inactive history does not violate uniqueness.
- [x] Preapproval stores explicit role/scope without creating a placeholder user; expiry is checked in code.
- [x] Private typed methods reject invalid transitions/parents and do not perform workflow approval without the service transaction.

#### Verification

Isolated partial-index/transition tests, expiry and concurrent request fixtures.

#### Implementation Notes

TICKET-025/026 own approval decisions and consumption; no new resubmission policy.

#### Completion Evidence

Application code authored by the parent agent; tests authored only by GPT-6 Luna max as requested. Added src/goodshowroom_ai/storage/access_requests.py; updated storage/models.py and storage/mongo.py; added tests/test_access_request_storage.py. Updated README.md, Tickets.md and planning/PROGRESS.md. No dependency/lock, environment, credential, frontend or sibling repository change.

Strict, versionless access-request/preapproval records store UUID references, normalized email, bounded typed scope, UTC dates and reviewer metadata. Active means pending. Expiry is checked in code at the exact boundary. Requests require an existing pending user with matching email and creator identity; preapproval requires an active role and conservative scope narrowing, without creating a user. Invalid source/role parents deny. Repeated pending rows are returned before insert; history denies resubmission until policy approval. A duplicate-key race poisons the transaction and requires retry in a fresh transaction. Close methods permit only rejection/revocation with an expected pending/active predicate. Approval and consumption remain TICKET-025/026 service workflows; no access is granted here.

Named methods require typed internal capabilities. Writes require the existing transaction fence before audit, increment policy_version once per writing transaction, and retain required atomic audit checks. Explicit index definitions add partial uniqueness on (kind,email_key) where active=true and a status/created_at index. Admin-list storage queries bound rows, encoded BSON data and cursor execution time, using UUID keyset pagination. No HTTP routes or admin screens added. Future services still enforce authenticated/admin roles, scope checks and reviewed decision workflows.

Final verification: uv run --locked ruff check . passed; uv run --locked ruff format --check . passed (16 files); uv run --locked pytest passed 85 tests in 24.56 seconds, exit 0. This includes 71 existing cases and 14 new fake-storage cases. Focused tests also passed 14 cases. Initial focused runs failed seven fixture assertions (query support, reviewer metadata, strict deprecation and setup uniqueness); test fixtures were corrected and settled-file runs passed. No production defect was confirmed by those failures.

The fake tests cover duplicate/history behavior, partial-index definitions, a simulated race/retry, expiry, invalid parents/transitions, role narrowing, capability denial, fence/audit order, policy increments and pagination bounds. They do not prove real MongoDB partial uniqueness, concurrent writers, transaction rollback or indexes. No verified isolated target was used; no live database or index was changed. The combined one-active-row uniqueness criterion stays unchecked pending real evidence. This is a review limit, not a requirement for new credentials or infrastructure before unrelated work.

Security review: fixed app DB target retained; no source-user/password import; scoped preapproval cannot exceed a single encompassing role grant; arbitrary transitions and resubmission deny; fields are bounded and unknown fields reject; safe errors omit raw driver details. Existing strict offline guards and Windows per-run temp/cache fix remain enabled. Await user review before Done.


User review: TICKET-023 approved with its recorded real-Mongo limits and marked Done.

### TICKET-024: Persist login validity and revocation state

- Type: DATABASE
- Priority: Critical
- Status: Done
- Milestone: App-data foundation
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-022
- Estimated Complexity: M

#### Description

Store the backend validity metadata needed by the identity bridge.

#### Implementation Scope

Login generation/absolute expiry/lease/revocation, exchange replay identity and login-session indexes; no conversation contents.

#### Acceptance Criteria

- [x] Eight-hour absolute limit, five-minute maximum lease and originating auth expiry are enforced independently of token skew/TTL.
- [x] Renewal cannot revive revoked/expired generations; logout retains replay bindings through assertion lifetime/tolerance.
- [ ] Revocation conflicts with protected commits through the shared fence and audit; no raw cookies/Google/internal tokens persist.

#### Verification

Frozen-clock expiry/renewal/replay tests and isolated revocation-versus-commit races.

#### Implementation Notes

TICKET-001 verifies signed callers; TICKET-010 deletes temporary context; this repository exposes no anonymous routes.

#### Completion Evidence

Parent authored application code; GPT-6 Luna max authored tests only. Added storage/logins.py, updated storage/models.py and storage/mongo.py; added tests/test_login_storage.py, extended shared fake/helpers in tests/test_identity_storage.py, updated the preexisting tests/__init__.py docstring. Updated README, Schema review note, Tickets and Progress. No new dependencies, configuration/credential edits, frontend changes or source-repository changes.

LoginRecord stores only UUID identity/generation/version and UTC validity metadata. Absolute expiry is bounded by the original verified auth_started_at plus eight hours and auth_expires_at. A lease is at most five minutes and never exceeds absolute/auth expiry. Typed owner-bound methods expose exchange, read, renewal and revocation. Replay resolves the same UUID binding without extending it; renewal cannot revive an expired/revoked generation or change originating auth expiry. Expected-version replacement rejects stale renewal/revocation; logout is idempotent for the same generation. Required fence and audit share the transaction/session; revocation retains original expiry and replay metadata. No raw cookies, Google tokens, internal assertions or conversation content are accepted.

Visible refinement for review: Schema's original TTL on expires_at is replaced in this ticket's explicit index definition by purge_after >= expires_at + 150 seconds. It retains replay bindings through assertions issued near absolute expiry (120-second assertion plus 30-second tolerance), while access still expires exactly at expires_at. auth_started_at/auth_expires_at bind the originating session and stop TTL deletion/retry from resetting the eight-hour cap. The Schema note preserves the original approved text and identifies this refinement. No TTL/index was installed or migrated. Existing expires_at TTL must not coexist with the proposed purge_after TTL; future setup must inspect/review existing indexes first.

Final commands: uv run --locked ruff check . passed; uv run --locked ruff format --check . passed (18 files); uv run --locked pytest passed 103 cases in 20.34 seconds, exit 0. All 85 previous cases passed plus 18 new login cases (15 functions with parametrization). Focused login/identity run passed 31. A formatting failure in the new test file was corrected before the final run. Frozen-clock fake-storage checks cover expiry boundaries, original-start cap, replay after renewal, retention, invalid fields, capability/ownership/generation checks, CAS conflicts, audit failure and required-audit/session checks.

Real isolated Mongo revocation-versus-commit races, rollback, TTL deletion and actual indexes remain unverified; the corresponding combined criterion stays unchecked. No live DB was contacted or mutated, and no verified isolated target was available. Signed caller verification is TICKET-001; temporary cancellation/removal is TICKET-010. These methods alone do not provide those workflows. Security review: fail-closed bounds, typed owners/capabilities, no secret-bearing fields, original-session binding, immutable expiry on revoke and poisoned transactions protect the storage boundary; compromised credentials/application bypass remain outside this protection. Await review before Done.


### TICKET-025: Implement identity bootstrap and access-request decisions

- Type: BACKEND
- Priority: Critical
- Status: Backlog
- Milestone: Access workflows
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-001, TICKET-002, TICKET-023
- Estimated Complexity: M

#### Description

Apply first-admin, preapproval-consumption and request-decision rules atomically.

#### Implementation Scope

Verified-provider user resolution, one-time bootstrap, preapproval binding and admin approve/reject transactions; no HTTP/UI wiring.

#### Acceptance Criteria

- [ ] Only the configured verified Google identity can bootstrap once; concurrent attempts cannot create extra initial admins.
- [ ] Preapproval consumption closes the pending request and grants its reviewed role/scope atomically; email collisions never merge.
- [ ] Admin decisions require current authority/expected policy; stale, self-approval and invalid-scope attempts fail with no partial user/request/audit changes.

#### Verification

Synthetic identity and isolated bootstrap/consumption/decision races with failure injection.

#### Implementation Notes

Uses existing repositories/policy, removing the former implicit bootstrap dependency inside TICKET-001.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-026: Implement role and employee-access changes

- Type: BACKEND
- Priority: Critical
- Status: Backlog
- Milestone: Access workflows
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-002, TICKET-022, TICKET-023, TICKET-025
- Estimated Complexity: M

#### Description

Provide transactional services for administrator-managed grants.

#### Implementation Scope

Role create/edit/deactivation, employee grant/revocation and preapproval create/revoke; API/UI owned separately.

#### Acceptance Criteria

- [ ] Effective user scope only narrows role grants and never exceeds grant-management authority.
- [ ] Expected resource/policy versions, last-active-admin and self-escalation safeguards hold under concurrent changes.
- [ ] Changes and required audits bump the shared policy atomically; assigned roles cannot be hard-deleted; no source data changes.

#### Verification

Role/scope matrices, concurrent last-admin/revocation and audit rollback tests.

#### Implementation Notes

Preapproval consumption remains TICKET-025; no invented default unrestricted administrator data access.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-027: Persist verified staff mappings and typed settings

- Type: DATABASE
- Priority: High
- Status: Backlog
- Milestone: App-data foundation
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-022
- Estimated Complexity: M

#### Description

Store explicit staff/source identity mappings and approved app settings.

#### Implementation Scope

Identity mapping models/source-qualified unique index and typed site/timezone/retention/disclosure setting documents; private methods only.

#### Acceptance Criteria

- [ ] Ambiguous/name-only identities, duplicate bindings and unsupported settings cannot grant access.
- [ ] Mapping/settings mutation helpers require verified service context, expected policy, shared fence and audit.
- [ ] Missing business/retention/disclosure values remain unset/disabled; no arbitrary query/metric definitions or secrets are stored.

#### Verification

Mapping uniqueness, version/fence/audit and safe-default fixtures on isolated data.

#### Implementation Notes

AuthorizationService reads these records; public validation/mutation and selectors are TICKET-035.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-028: Create and list owned saved-chat sessions

- Type: DATABASE
- Priority: High
- Status: Backlog
- Milestone: Saved-chat storage
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-002, TICKET-021, TICKET-057
- Estimated Complexity: S

#### Description

Provide the saved-chat parent records needed by atomic turns.

#### Implementation Scope

Chat-session model, owner-bound create/replay/list, safe titles, initial version and app-only indexes; no turns/history API.

#### Acceptance Criteria

- [ ] Same owner/request UUID recovers the same chat; another owner's collision never reveals or reuses it.
- [ ] Version starts at zero; stable authorized cursor listing reveals only safe owned metadata.
- [ ] Deleting/expired parents deny new work; temporary mode never creates a row and all storage bounds apply.

#### Verification

Create/replay/collision, owner/list pagination and parent-state tests.

#### Implementation Notes

TICKET-008 inserts turns; TICKET-029 handles complete history/deletion.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-029: Read and delete saved history safely

- Type: DATABASE
- Priority: High
- Status: Backlog
- Milestone: Saved-chat storage
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-008, TICKET-028
- Estimated Complexity: M

#### Description

Provide current-grant saved-history reads and derivative cleanup.

#### Implementation Scope

Version/snapshot-bound turn pagination, safe owner/admin read services, audited review and idempotent parent/turn cleanup.

#### Acceptance Criteria

- [ ] Every returned message/fact/summary/visual passes current ownership/data grants; unsafe content is omitted with safe notices.
- [ ] Admin review requires its own capability/audit, never impersonates the owner or changes agent context; required audit failure denies review.
- [ ] Deletion marks parent deleting before removing all derivatives, blocks commits and safely resumes partial cleanup; no guessed TTL/retention or operational writes.

#### Verification

Real isolated read/delete/commit races, revoked-history and failed-review-audit tests.

#### Implementation Notes

Authenticated encrypted cursors come from TICKET-057 when exposed by routes; no temporary transcript repository.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-030: Define shared API schemas and safe errors

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: API foundation
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-017, TICKET-018, TICKET-019, TICKET-020, TICKET-022
- Estimated Complexity: S

#### Description

Make backend payload shapes and frontend contracts agree.

#### Implementation Scope

Typed request/result/evidence envelopes, error mapping, UTC/UUID encoding and OpenAPI contract export; no business endpoints.

#### Acceptance Criteria

- [ ] Strict schemas reject unknown/invalid/deep inputs without reflecting raw values; safe errors match approved HTTP codes.
- [ ] Facts/evidence/datasets/visual references validate consistently; zero, partial, unavailable and failures remain distinct.
- [ ] Frontend type/runtime fixtures match exported schemas; no internal token/driver/trace field enters public envelopes.

#### Verification

Round-trip/schema-drift fixtures and malformed/error payload tests.

#### Implementation Notes

TICKET-057 owns ingress/admission/cursor mechanics; no additional schema-generator service.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-031: Connect Google login to backend identity routes

- Type: INTEGRATION
- Priority: Critical
- Status: Backlog
- Milestone: Identity integration
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [Tech Stack](docs/05-TECH-STACK.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-001, TICKET-018, TICKET-020, TICKET-025, TICKET-030, TICKET-057
- Estimated Complexity: M

#### Description

Wire NextAuth Google and the approved server-only login exchange.

#### Implementation Scope

Google callback/server session binding, internal exchange/renew/revoke routes and backend login metadata; no account screens or generic proxy.

#### Acceptance Criteria

- [ ] Verified provider identity binds app user/login generation; callback cancellation, email collision and invalid assertions fail safely.
- [ ] Exchange/renew/revoke honor purpose/replay, absolute auth lifetime and lease rules; private tokens never reach the browser.
- [ ] Google library protections/callbacks and required settings are tested with synthetic callbacks; actual Google verification is separately labeled/authorized.

#### Verification

Real signing validators, callback/server-route contract and renewal/revocation tests with mocked Google.

#### Implementation Notes

Preserve stable NextAuth V4 and existing secrets; no provider migration or automatic Atlas/key setup.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-032: Expose own access status and request API

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Access APIs
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-023, TICKET-025, TICKET-030, TICKET-057
- Estimated Complexity: S

#### Description

Provide the approved /me and access-request operations.

#### Implementation Scope

Own status/UI capability choices and authenticated request submission; no administrator endpoints.

#### Acceptance Criteria

- [ ] Pending/rejected/revoked users receive only their own safe status, never business data.
- [ ] Duplicate active submissions return the existing request; rejected/revoked resubmission stays unavailable without approved policy.
- [ ] Backend resolves caller identity/current grants; spoofed target IDs, role fields, malformed bodies and rate excess fail safely.

#### Verification

Route allow/deny/duplicate/status/error and admission tests.

#### Implementation Notes

No email/body field selects another user; Google authentication alone does not grant access.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-033: Expose request-review and preapproval APIs

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Access APIs
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-025, TICKET-026, TICKET-030, TICKET-057
- Estimated Complexity: S

#### Description

Wire administrator access requests and email preapprovals.

#### Implementation Scope

List/review requests and create/revoke preapprovals through existing transactional services.

#### Acceptance Criteria

- [ ] Every route requires current management capability; selectors never bypass field/record restrictions.
- [ ] Approve/reject/preapprove/revoke return safe validated results; required policy versions and scope validation are enforced.
- [ ] Stale edits, duplicate races and required-audit failures cause no partial state or secret/identity leakage.

#### Verification

Authenticated route matrix, pagination, conflict and service-fault injection tests.

#### Implementation Notes

No service logic duplicated in routes; no unreviewed resubmission behavior.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-034: Expose role and employee-access APIs

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Access APIs
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-026, TICKET-030, TICKET-057
- Estimated Complexity: S

#### Description

Wire the approved administrator users/roles operations.

#### Implementation Scope

Bounded users/roles lists and role create/edit/deactivate plus user access edits; no identity-mapping routes.

#### Acceptance Criteria

- [ ] Typed explicit grants, current management authority and both required version checks protect mutations.
- [ ] Cross-scope selections, self-escalation and concurrent last-admin loss are rejected without partial changes.
- [ ] Responses expose only authorized management fields and safe errors; audit failures deny required mutations.

#### Verification

Route/policy/version matrices and concurrent critical-edit tests.

#### Implementation Notes

Administration remains separate from operational data grants; no role hard-delete endpoint.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-035: Expose access choices, mappings and settings APIs

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Access APIs
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-002, TICKET-003, TICKET-027, TICKET-030, TICKET-057
- Estimated Complexity: M

#### Description

Wire bounded grant selectors and approved configuration editing.

#### Implementation Scope

/admin/access-options, user identity mappings and typed settings GET/PATCH using private repositories/services.

#### Acceptance Criteria

- [ ] Selectors reveal only permitted dataset/site/staff/field/metric/record choices; no arbitrary DB browser/query is accepted.
- [ ] Mapping/settings changes verify authority, source identity and expected policy; fence/audit updates are atomic.
- [ ] Unknown retention/disclosure values cannot silently enable behavior; source records/settings containing credentials never reach clients.

#### Verification

Selector projection/scope tests, mapping conflict and settings/version/audit failures.

#### Implementation Notes

Only approved typed settings; metric meanings remain trusted code, not editable prompts or arbitrary policy JSON.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-036: Expose saved-turn execution and recovery API

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Chat APIs
- Requirement Reference: [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-009, TICKET-028, TICKET-030, TICKET-057
- Estimated Complexity: M

#### Description

Wire saved message execution and request-status recovery.

#### Implementation Scope

POST saved turns and GET request status; call the agent/ResponseBuilder/commit services rather than duplicate them.

#### Acceptance Criteria

- [ ] Caller/session/owner and expected versions are checked; matching committed retries resolve before stale-version rejection.
- [ ] Only validated committed currently releasable answers return success; unknown save stays unknown until reconciled.
- [ ] Concurrent/changed-payload requests, disconnects and mid-run revocation do not create duplicate turns, stale release or raw logs.

#### Verification

HTTP contract/fault-injection tests across model, commit and lost-response boundaries.

#### Implementation Notes

Non-streaming; no client history, model settings, arbitrary tools or raw DB queries.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-037: Expose saved-chat management and history APIs

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Chat APIs
- Requirement Reference: [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-029, TICKET-030, TICKET-057
- Estimated Complexity: S

#### Description

Wire owned saved-chat creation, listing, history and deletion.

#### Implementation Scope

Approved saved-chat CRUD subset, safe titles, authorized cursor pagination and cleanup outcomes.

#### Acceptance Criteria

- [ ] Create/replay/list/history are owner-bound; missing and another owner's resource have the same safe response.
- [ ] Current grants filter every history item; pagination rejects tampering/expired/stale-policy cursors and does not invent totals.
- [ ] Delete returns success only after confirmed cleanup, otherwise safe cleanup_pending; repeating deletion cannot resurrect or expose content.

#### Verification

Owner/revocation/cursor/cleanup route tests against isolated data.

#### Implementation Notes

No edit/export endpoint; no temporary chats in saved lists.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-038: Expose audited administrator conversation review

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Review APIs
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-029, TICKET-030, TICKET-057
- Estimated Complexity: S

#### Description

Wire separate saved-message review endpoints.

#### Implementation Scope

Admin conversation list and ordered turns with approved bounded user/date filters.

#### Acceptance Criteria

- [ ] Review capability and current data grants are checked independently; required audit failure denies review.
- [ ] Protected messages/facts/visuals are safely filtered; no impersonation or write path is offered.
- [ ] Temporary sessions, internal trace data/links and forbidden identities/counts never appear.

#### Verification

Admin/non-admin/grant-change matrices and audit/temporary-exclusion route tests.

#### Implementation Notes

Read-only review never populates the administrator's agent context.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-039: Expose memory-only Temporary Chat APIs

- Type: BACKEND
- Priority: High
- Status: Backlog
- Milestone: Chat APIs
- Requirement Reference: [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-010, TICKET-030, TICKET-057
- Estimated Complexity: S

#### Description

Wire temporary create/turn/status/end without a durable fallback.

#### Implementation Scope

Mode-bound memory-only endpoints, user/login ownership, safe unavailability and cancellation.

#### Acceptance Criteria

- [ ] Success reports not_saved and advances only memory; duplicate/retry status remains memory-only.
- [ ] Expired, unknown and cross-user IDs reveal no existence difference; end is idempotent and late responses cannot restore state.
- [ ] All failure/logout/expiry paths preserve non-persistence and safe sanitized access logging; no list/review/history endpoint exists.

#### Verification

API/memory/storage sentinel tests with fake clock, failures and in-flight cancellation.

#### Implementation Notes

One backend process; no queue/cache/store or SDK persistent-session fallback.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-040: Render safe answers, cards, charts and tables

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Chat UI components
- Requirement Reference: [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-007, TICKET-018, TICKET-020, TICKET-030
- Estimated Complexity: M

#### Description

Build reusable renderers for the approved structured answer envelope.

#### Implementation Scope

shadcn components, Recharts, semantic tables and restricted react-markdown/optional remark-gfm; no network/session wiring.

#### Acceptance Criteria

- [ ] Only approved validated visual kinds render; facts/labels/units/coverage/source/timezone/freshness remain consistent.
- [ ] Raw HTML/MDX, images/iframes, unsafe URLs and arbitrary chart code/CSS cannot execute or leak data.
- [ ] Zero/partial/clarification/unavailable/errors have distinct accessible states and chart text/table alternatives.

#### Verification

Component fixtures, hostile Markdown/chart payloads and exact text/data assertions.

#### Implementation Notes

No downloads, streaming UI SDK or browser-side business calculations.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-041: Build the responsive chat workspace shell

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Chat UI components
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-018, TICKET-020
- Estimated Complexity: S

#### Description

Create reusable navigation, header and composer layout.

#### Implementation Scope

Saved-chat sidebar/mobile sheet, mode/scope labels and multiline composer; synthetic props only.

#### Acceptance Criteria

- [ ] Desktop/mobile layouts preserve readable content and keyboard focus; controls remain clear of the composer/keyboard.
- [ ] Enter/Shift+Enter/IME behavior and 4,000-character bounds are correct; duplicate submission can be disabled by parent state.
- [ ] No protected data or simulated business success is exposed by the shell; no persistent chat storage/cache is introduced.

#### Verification

Component keyboard/IME/viewport tests and accessibility checklist.

#### Implementation Notes

Authentication, real history and turn transport are separate tickets.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-042: Build login and own access-status screens

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Access UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-031, TICKET-032, TICKET-041, TICKET-056
- Estimated Complexity: S

#### Description

Connect Google login and account approval status to the interface.

#### Implementation Scope

Continue with Google, cancellation/error, request submission and pending/rejected/revoked/preapproved routing.

#### Acceptance Criteria

- [ ] Unapproved users see only their own status/request controls; hidden navigation never substitutes for backend denial.
- [ ] Request submission handles duplicate/conflict/rate states without invented resubmission behavior.
- [ ] Session loss clears protected working state and returns to sign-in/status; no internal token enters browser state/logs.

#### Verification

Synthetic callback/status component and protected-page route tests.

#### Implementation Notes

Actual Google sign-in checks require separately labeled designated-account evidence.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-043: Build access-request and preapproval screens

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Admin UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-033, TICKET-044
- Estimated Complexity: M

#### Description

Let authorized administrators review requests and preapprove email access.

#### Implementation Scope

Bounded lists, approve/reject form and preapproval create/revoke, reusing grant controls.

#### Acceptance Criteria

- [ ] Only authorized management views/actions appear; selected role/narrowing scope is previewed and validated by the server.
- [ ] Saving requires confirmation and current versions; stale edits reload rather than overwrite.
- [ ] Pending/duplicate/failed-audit/last-admin/denied outcomes are explicit; no success notice appears before confirmed save.

#### Verification

Allowed/denied/form/conflict component tests and synthetic API integration.

#### Implementation Notes

No public bootstrap button; no source-data edits or trace viewer.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-044: Build role and employee-grant screens

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Admin UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-034, TICKET-035, TICKET-042
- Estimated Complexity: M

#### Description

Provide explicit typed role/user-access editing.

#### Implementation Scope

Role/user lists, reusable grant editor, role deactivation and employee grant/revocation confirmation.

#### Acceptance Criteria

- [ ] Dataset/site/record/field/metric choices come from authorized API options; blank selections never mean all.
- [ ] Preview explains role maximum versus individual narrowing; only confirmed server results represent effective access.
- [ ] Version conflicts, missing staff scope, self-escalation and last-admin rejection preserve safe state and show clear errors.

#### Verification

Grant-form matrices, keyboard navigation and conflict/revocation component tests.

#### Implementation Notes

No arbitrary policy JSON, wildcards or assumed unrestricted administrator business access.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-045: Build staff mapping and approved-settings screens

- Type: FRONTEND
- Priority: Medium
- Status: Backlog
- Milestone: Admin UI
- Requirement Reference: [Schema](docs/06-DATABASE-SCHEMA.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-035, TICKET-044
- Estimated Complexity: S

#### Description

Expose verified mappings and bounded app settings to authorized administrators.

#### Implementation Scope

Staff/source chooser and only API-approved site/timezone/retention/disclosure settings.

#### Acceptance Criteria

- [ ] Ambiguous/unverified mappings cannot be saved as ownership proof; version/conflict results are handled.
- [ ] Unreviewed policy values stay visibly unavailable; inputs cannot redefine metrics or submit queries/secrets.
- [ ] Changes require confirmation and show success only after audited server save; navigation obeys current capability.

#### Verification

Selector/settings input, denial and stale-policy component tests.

#### Implementation Notes

No new business policy values or setup prerequisites are introduced.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-046: Connect saved messages and safe retry states

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: First question UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-036, TICKET-037, TICKET-040, TICKET-041, TICKET-042, TICKET-056
- Estimated Complexity: M

#### Description

Send saved-chat questions and display validated complete answers.

#### Implementation Scope

Owned chat creation, request UUID/expected version, one active turn, renderer integration and status recovery.

#### Acceptance Criteria

- [ ] A result is labeled Saved only after committed success; unknown save triggers status lookup, never a blind duplicate run.
- [ ] Changed/stale/in-flight requests and access loss show safe recovery; late responses cannot populate another user/chat.
- [ ] Loading is truthful and non-streaming; browser working memory has no internal tokens/persistent transcript or invented progress.

#### Verification

Component/transport fault tests for commit, disconnect, retry, version and account-switch races.

#### Implementation Notes

Source queries/calculations remain backend-owned; no client-supplied history or model configuration.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-047: Connect saved history, resume and deletion

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: First question UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-037, TICKET-046
- Estimated Complexity: S

#### Description

Provide owned history selection and reopening with current grants.

#### Implementation Scope

Sidebar pagination, ordered turn display, snapshot labels, resume version state and confirmed deletion.

#### Acceptance Criteria

- [ ] History uses server-filtered items and safe titles; permission changes remove stale protected views.
- [ ] Reopened answers are dated snapshots and fresh questions create new turns; cursor/version errors reload safely.
- [ ] Deletion requires confirmation; cleanup_pending stays unresolved and removed chats/late results cannot reappear.

#### Verification

History/revocation/pagination/delete component and transport tests.

#### Implementation Notes

No exports, employee-to-employee history access or persistent browser cache.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-048: Connect Temporary Chat and coordinated sign-out

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Temporary UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-031, TICKET-039, TICKET-046
- Estimated Complexity: M

#### Description

Expose temporary mode without saving or copying its content.

#### Implementation Scope

Separate temporary creation/turn/retry, persistent privacy label, end action and backend/NextAuth sign-out coordination.

#### Acceptance Criteria

- [ ] Mode switching starts a new chat; temporary content never appears in saved history/browser persistence or recorded artifacts.
- [ ] End/logout/expiry/unavailability clears working state and ignores late results; no silent saved-mode fallback.
- [ ] Notices distinguish no saved transcript from metadata diagnostics/provider retention and disclose unconfirmed remote cleanup accurately.

#### Verification

Memory-only component/browser-storage sentinel tests, fake expiry and notification-failure scenarios.

#### Implementation Notes

Follow AGENTS.md for any authorized browser checks; no real private transcript in evidence.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-049: Build administrator saved-conversation review

- Type: FRONTEND
- Priority: High
- Status: Backlog
- Milestone: Admin review UI
- Requirement Reference: [User Flows](docs/03-USER-FLOWS.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md)
- Dependencies: TICKET-038, TICKET-040, TICKET-042
- Estimated Complexity: S

#### Description

Display the separate audited saved-history review surface.

#### Implementation Scope

Bounded user/date/session selection, safe filtered messages and dated visuals; no composer into employee chats.

#### Acceptance Criteria

- [ ] Only current review-capable administrators see permitted content; API denial/revocation clears protected views.
- [ ] Temporary chats and trace contents/links are absent; review cannot impersonate or import another user's context.
- [ ] Saved-review/privacy notices, empty/partial/audit-failure and pagination states are accessible and truthful.

#### Verification

Review capability/field-filter/temporary-exclusion component tests and synthetic route integration.

#### Implementation Notes

Operational grants still apply to reviewed facts even when saved-message review is allowed.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-050: Add Q-02 period counts and comparisons

- Type: BACKEND
- Priority: Medium
- Status: Backlog
- Milestone: Additional lead questions
- Requirement Reference: [Scope](docs/02-SCOPE.md); [Architecture](docs/04-ARCHITECTURE.md); [API Contracts](docs/07-API-CONTRACTS.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-005, TICKET-014, TICKET-054
- Estimated Complexity: S

#### Description

Extend the reviewed count metric to today and month comparisons.

#### Implementation Scope

Typed approved period resolution/comparison and evidence; no new data sources or conversion meanings.

#### Acceptance Criteria

- [ ] Today/calendar months/explicit rolling periods use reviewed timezone/UTC/DST rules and exact permitted counts.
- [ ] Ambiguous cross-site timezone or unreliable dates clarify/disclose coverage instead of fabricating totals.
- [ ] Four Q-02 phrasings plus boundary/follow-up cases join the shared corpus; text/comparison datasets agree exactly.

#### Verification

Frozen independent-oracle date/count fixtures and corpus regression.

#### Implementation Notes

Reuse MetricRegistry/ResponseBuilder; feature enablement requires reviewed real source/date rules.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-051: Add Q-03 status and source breakdowns

- Type: BACKEND
- Priority: Medium
- Status: Backlog
- Milestone: Additional lead questions
- Requirement Reference: [Scope](docs/02-SCOPE.md); [Architecture](docs/04-ARCHITECTURE.md); [API Contracts](docs/07-API-CONTRACTS.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-005, TICKET-006, TICKET-014, TICKET-054
- Estimated Complexity: S

#### Description

Provide reviewed status/source grouping on authorized leads.

#### Implementation Scope

Trusted mappings, approved grouping, coverage/suppression and bounded bar/table data.

#### Acceptance Criteria

- [ ] Record and grouping-field permissions apply before deterministic grouping; totals never use a truncated page.
- [ ] Unknown mappings and suppressed groups are disclosed without guessing or revealing complementary hidden counts.
- [ ] Four Q-03 phrasings and adversarial/partial cases extend the corpus; undefined disclosure policy withholds the sensitive breakdown.

#### Verification

Exact group/count/data fixtures and small-group/differencing regression.

#### Implementation Notes

No new status semantics or threshold is invented by the agent or this ticket.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-052: Add Q-04 assigned and pending lead lists

- Type: BACKEND
- Priority: Medium
- Status: Backlog
- Milestone: Additional lead questions
- Requirement Reference: [Scope](docs/02-SCOPE.md); [Architecture](docs/04-ARCHITECTURE.md); [API Contracts](docs/07-API-CONTRACTS.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-003, TICKET-004, TICKET-007, TICKET-014, TICKET-027, TICKET-054, TICKET-057
- Estimated Complexity: M

#### Description

List permitted assigned/pending leads using verified identity and lifecycle rules.

#### Implementation Scope

Named read tool, reviewed staff/status mappings, projected fields and authorized cursor pagination.

#### Acceptance Criteria

- [ ] My leads requires verified source staff/site mapping; missing/ambiguous mapping never broadens access.
- [ ] Pending uses only approved lifecycle meaning; undefined meaning asks clarification or remains unavailable.
- [ ] Four Q-04 phrasings and cross-user/cursor/field tests extend the corpus; bounded table rows and metadata are exact.

#### Verification

Independent list/mapping/status fixtures, pagination and unauthorized record tests.

#### Implementation Notes

No operational updates, free-form Mongo filters or guessed ownership from names/phones.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-053: Add Q-05 permitted site and employee comparisons

- Type: BACKEND
- Priority: Medium
- Status: Backlog
- Milestone: Additional lead questions
- Requirement Reference: [Scope](docs/02-SCOPE.md); [Architecture](docs/04-ARCHITECTURE.md); [API Contracts](docs/07-API-CONTRACTS.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-005, TICKET-006, TICKET-014, TICKET-027, TICKET-054
- Estimated Complexity: M

#### Description

Compare only explicitly permitted verified site/staff groups.

#### Implementation Scope

Trusted group mappings, resolved reporting timezone, aggregate visibility and matching comparative datasets.

#### Acceptance Criteria

- [ ] Each group/source/field is authorized; forbidden groups are not silently omitted to present a requested total.
- [ ] Unresolved mappings/timezone/disclosure produce clarification/withholding with no guessed values.
- [ ] Four Q-05 phrasings and overlap/complement/differencing cases extend the corpus; cards/bars/tables match the same facts.

#### Verification

Cross-site/staff policy matrices and independent expected comparison fixtures.

#### Implementation Notes

No enterprise-wide reporting permission or new analytics scope is inferred from an admin role.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-054: Verify the first saved-question vertical slice

- Type: TEST
- Priority: High
- Status: Backlog
- Milestone: First end-to-end milestone
- Requirement Reference: [PRD](docs/01-PRD.md); [User Flows](docs/03-USER-FLOWS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-014, TICKET-033, TICKET-034, TICKET-042, TICKET-046, TICKET-047
- Estimated Complexity: M

#### Description

Prove the approved first lead question works through both local services.

#### Implementation Scope

Synthetic Google/provider callback, approved/preapproved user, last-seven-days card, follow-up and saved resume.

#### Acceptance Criteria

- [ ] Protected Next.js/FastAPI flow produces exact fixture facts/period/timezone/coverage and matching card; denied user receives no data.
- [ ] Follow-up refreshes facts within allowed scope, committed turns resume without duplicates and saved-review notice is visible.
- [ ] Record real static/component/integration/journey results; distinguish mocked Google/model from actual provider evidence and list pending checks.

#### Verification

Approved local suites plus user browser checklist or explicitly authorized agent browser verification.

#### Implementation Notes

No mandatory paid/live-provider run, new credentials, source writes or production-readiness claim.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-055: Verify complete local version-one journeys

- Type: TEST
- Priority: High
- Status: Backlog
- Milestone: Local release verification
- Requirement Reference: [PRD](docs/01-PRD.md); [Scope](docs/02-SCOPE.md); [User Flows](docs/03-USER-FLOWS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-015, TICKET-016, TICKET-043, TICKET-045, TICKET-048, TICKET-049, TICKET-050, TICKET-051, TICKET-052, TICKET-053, TICKET-054, TICKET-056, TICKET-057, TICKET-058
- Estimated Complexity: M

#### Description

Run cross-service acceptance for all enabled approved release capabilities.

#### Implementation Scope

Admin access/settings/review, Q-01–Q-05, saved/temporary lifecycle, mobile/keyboard/error/privacy regression and optional integration evidence review.

#### Acceptance Criteria

- [ ] All enabled capabilities pass their acceptance/security/numerical cases; undefined business settings remain unavailable and explicitly recorded.
- [ ] Permission changes, duplicate/interrupted work, cleanup failure, injection and temporary/log/trace sentinels have reproducible safe outcomes.
- [ ] Evidence lists pass/fail/skips, supported browser scope, manual review and outstanding real-provider/MFA/retention checks; unmet required criteria are not marked Done.

#### Verification

Full approved isolated suites and user/manual checklist; live checks only with explicit authorization/budget.

#### Implementation Notes

Local acceptance only; no deployment, external monitoring or assurance inferred from mocks.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-056: Harden Next.js server proxy and browser routes

- Type: SECURITY
- Priority: Critical
- Status: Backlog
- Milestone: Web security
- Requirement Reference: [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-018, TICKET-020, TICKET-030, TICKET-031
- Estimated Complexity: M

#### Description

Enforce the server-only browser-to-backend boundary.

#### Implementation Scope

Fixed route forwarding, server-session assertion injection, custom CSRF/Origin checks, cookie/header/CSP and safe logging policy.

#### Acceptance Criteria

- [ ] Browser identity/roles/headers cannot replace verified server identity; no arbitrary target, wildcard CORS or token exposure is accepted.
- [ ] Mutations require session-bound CSRF and exact allowed origin; Host/forwarded headers, redirects and malformed requests fail safely.
- [ ] Protected responses are no-store; safe errors/CSP/rendering controls and default framework/auth/access logs exclude secrets and temporary identifiers.

#### Verification

Real validator/HTTP tests for forged origins/headers, CSRF, proxy escape, cookies and secret/client-bundle leakage.

#### Implementation Notes

NextAuth protections do not automatically secure custom routes; loopback HTTP remains a development exception.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-057: Enforce backend admission, limits and opaque cursors

- Type: SECURITY
- Priority: Critical
- Status: Backlog
- Milestone: API security
- Requirement Reference: [API Contracts](docs/07-API-CONTRACTS.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-002, TICKET-019, TICKET-030
- Estimated Complexity: M

#### Description

Apply approved bounded execution and safe pagination mechanics.

#### Implementation Scope

Pre-parse body cap, per-user admission, per-chat locking, deadlines/retry budgets, context/output constraints and authenticated encrypted cursors.

#### Acceptance Criteria

- [ ] Approved body/message/page/row/group/tool/token/time/rate limits reject safely before costly work; totals are never truncated into page counts.
- [ ] Cursor validation binds caller/filter/order/policy/expiry; tampering, cross-user reuse and stale grants cannot select arbitrary records.
- [ ] Fake-clock/boundary/cancellation tests prove no leaked locks, unsafe retries or persistent temporary state; failures reveal no raw input/secrets.

#### Verification

Just-below/at/above bounds, cursor attacks, overlapping requests and cancellation tests.

#### Implementation Notes

Private purpose-separated key configuration; one process, no Redis/queue/new service or custom cryptography.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.

### TICKET-058: Document verified local run and review procedures

- Type: DEVOPS
- Priority: Medium
- Status: Backlog
- Milestone: Local handoff
- Requirement Reference: [Tech Stack](docs/05-TECH-STACK.md); [API Contracts](docs/07-API-CONTRACTS.md); [UI/UX](docs/08-UI-UX-GUIDELINES.md); [Security](docs/09-SECURITY.md); [Test Strategy](docs/10-TEST-STRATEGY.md)
- Dependencies: TICKET-017, TICKET-018, TICKET-054
- Estimated Complexity: S

#### Description

Replace the README placeholder with accurate local operating guidance.

#### Implementation Scope

Actual package paths/start/check commands, non-secret environment names, callback/first-admin/key guidance, privacy/verification limits and owner-reviewed incident/revocation procedures.

#### Acceptance Criteria

- [ ] README commands are checked against implemented scripts and explain one-process temporary restart/expiry behavior.
- [ ] Setup preserves existing credentials, uses placeholders only and never launches/imports sibling services or source startup/index routines.
- [ ] Saved review/temporary/provider-diagnostic disclosures and manual/live-check limitations match tested behavior; no deployment/CI instructions are added.

#### Verification

Reproduce documented synthetic local workflow and inspect commands/references for secret or scope leakage.

#### Implementation Notes

No runtime credential provisioning by documentation; real provider settings remain owner-controlled.

#### Completion Evidence

Not started. No implementation or verification results. Record actual files, commands/results, limitations and required review before Done.







