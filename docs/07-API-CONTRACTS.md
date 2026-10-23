# Good Showroom AI Chatbot - API Contracts

Purpose: Define how the interface, backend, and approved read tools exchange validated information.

Status: Approved by User  
Updated: 2026-10-22  
Approval: The user approved API Contracts on 2026-10-22 and authorized UI/UX Guidelines only. No coding is authorized.
Basis: Approved [PRD](01-PRD.md), [Scope](02-SCOPE.md), [User Flows](03-USER-FLOWS.md), [Architecture](04-ARCHITECTURE.md), [Tech Stack](05-TECH-STACK.md), and [Database Schema](06-DATABASE-SCHEMA.md). Routes, payloads, and starting defaults are approved designs, not implemented endpoints or measured guarantees. Open technical values remain explicit.

Presentation/security approval (2026-10-22): The user approved [UI/UX Guidelines](08-UI-UX-GUIDELINES.md), including restricted Markdown for the existing `answer` string, and [Security](09-SECURITY.md), including token/byte/browser safeguards. API payload shape, numerical validation, and structured-data authority remain intact. These are approved designs, not implemented controls.

## 1. API Boundary and Common Rules

The browser calls Next.js `/api/v1/*` routes. Next.js verifies its NextAuth session and forwards only fixed, allowlisted operations to FastAPI `/v1/*`. NextAuth owns `/api/auth/*`. Internal login routes are server-only and have no general browser proxy.

- JSON uses `snake_case`, `schema_version: 1`, UUID app IDs, UTC ISO-8601 timestamps, and IANA timezones. Browser-supplied identity/grants are never authoritative.
- Success: `{schema_version, request_id, data}`. Failure: `{schema_version, request_id, error: {code, message, retryable}}`. Never return stack traces, raw validation inputs, secrets, or trace links. Validate request UUIDs; generate separate safe diagnostic IDs.
- Protected responses use `Cache-Control: no-store`. No persistent browser cache, request-body logging, arbitrary proxy targets, raw MongoDB query endpoints, downloads, or streaming.
- Lists return `{items, next_cursor}`. Opaque cursors bind caller, filters, stable ordering/ID tie-breaker, and policy version. Recheck access per page; permission changes invalidate cursors.

## 2. Identity and Session Contract

Next.js sends internal identity assertions through a server-only authorization header. Verify issuer, audience, allowed algorithm, `kid`, signature, `iat`/expiry, and purpose. Normal assertions bind `sub` (app user), `login_session_id`, and `generation`; current database approval/grants remain authoritative.

| Server-only route | Contract |
|---|---|
| `POST /v1/internal/login/exchange` | Signed bootstrap assertion: verified Google provider ID/email and NextAuth-session binding/expiry. Resolve app identity/login; apply first-admin/preapproval rules. Return user/login IDs and access status to Next.js only. |
| `POST /v1/internal/login/renew` | Fresh server verification of the same NextAuth session renews a still-valid, unrevoked login lease. It cannot extend beyond that session's expiry or revive a revoked generation. |
| `POST /v1/internal/login/revoke` | Idempotently revoke this login generation, cancel its work, and discard its temporary contexts. Return `204` only after invalidation succeeds. |

Exchange replay cannot create extra sessions, extend expiry, or repeat bootstrap. Email collisions never auto-merge. Unapproved users can request/check access and sign out; business/admin actions require approval and an active role.

Custom Next.js mutations require CSRF/origin checks. Logout coordinates backend revocation and NextAuth sign-out; clear browser state and report unconfirmed cleanup on failure. Starting bounds: assertion **120 seconds**, login lease **5 minutes**, capped by auth expiry. If notifications fail, reuse stops at lease expiry; memory removal follows within the cleanup sweep. Approved Security defines signing/rotation/replay rules and the absolute login cap; these bounds are untested.

## 3. Access and Administrator Routes

Paths below are relative to FastAPI `/v1`; browser counterparts use `/api/v1`.

| Method and path | Request/result |
|---|---|
| `GET /me` | Own account/access status, active role label, effective UI capabilities and allowed scope choices. No operational data for pending/rejected/revoked users. |
| `POST /access-requests` | Empty body; request for the authenticated identity. Return `201`, or existing pending request with `200`. No duplicate active request; rejected/revoked resubmission remains unavailable pending policy approval. |
| `GET /admin/access-requests` | Cursor/status-filtered request list; only access-management administrators. |
| `POST /admin/access-requests/{id}/decision` | `decision: approve/reject`; explicit `role_id`/`scope_limit` on approval. Return reviewed status. |
| `POST /admin/preapprovals` | Target email, `role_id`, typed `scope_limit`, optional approved expiry; `201`. `POST /admin/preapprovals/{id}/revoke` revokes an unused entry. |
| `GET /admin/users`, `GET /admin/roles` | Bounded access-management lists with editable resource versions. |
| `POST /admin/roles`, `PATCH /admin/roles/{id}` | Typed name/capabilities/grants; edits require `expected_version`. Return the resulting version. Deactivate through the edit route; no hard-delete of assigned roles. |
| `PATCH /admin/users/{id}/access` | `expected_version`, approval status, `role_id`, narrowing `scope_limit`; revocation allowed. Reject ordinary self-approval and loss of the last active administrator. |
| `GET /admin/access-options` | Safe allowlisted dataset/site/field/metric/record choices. No arbitrary DB browser; staff/record search uses fixed typed filters and current grants. |
| `PUT /admin/users/{id}/identity-mappings/{dataset_id}` | Verified staff handle/site mapping from allowed choices; audit reviewer, reject ambiguous/reused identities. |
| `GET/PATCH /admin/settings` | Only approved typed site/timezone/retention/disclosure settings; no secrets or arbitrary metric definitions. Unreviewed policy values remain disabled. |

Grants contain dataset/site IDs, record mode (`assigned`, `selected`, `all_permitted`), selected record handles, field/metric IDs, and capabilities. Backend resolves source references; no raw queries/operators/pipelines/wildcards. Individual grants narrow roles; administration does not grant all business data. Every admin mutation carries `expected_policy_version`; user/role edits also check their integer `expected_version`. Compare the shared policy fence and update/audit atomically. Return new versions; stale edits get `409`. This uses the approved Schema without adding access-request version fields.

## 4. Chat and History Routes

| Method and path | Contract |
|---|---|
| `POST /saved-chats` | `{request_id}`; create/recover the same owned chat, `201`/`200`, `mode:saved`. New version is `0`; replay returns current version. Use UUID as creation identity; never recover another owner's collision. |
| `GET /saved-chats` | Own saved chats only; paginated safe titles and versions. |
| `GET /saved-chats/{id}/turns` | Ordered complete turns only; current ownership/data checks and snapshot/version-bound pagination. Restricted old content is omitted or replaced by a safe notice. |
| `POST /saved-chats/{id}/turns` | Turn request below; return `200` only with a validated, committed, currently releasable response. |
| `GET /saved-chats/{id}/requests/{request_id}` | Recovery status: `processing/committed/failed/interrupted/unknown`. Include a completed response only after current authorization; unknown is never permission to rerun blindly. No background job is created. |
| `DELETE /saved-chats/{id}` | Owner only; block new commits and remove all derivatives. `204` after completion; `503 cleanup_pending` if incomplete. Repeated deletion safely resumes cleanup. |
| `POST /temporary-chats` | `{request_id}`; create/recover an in-memory chat bound to this user/login; `mode:temporary`, version `0`. No saved-chat row. |
| `POST /temporary-chats/{id}/turns` | Same turn shape; validated memory-only update, `persistence:not_saved`. |
| `GET /temporary-chats/{id}/requests/{request_id}` | Same recovery states in memory only; no durable history/recovery after end or restart. |
| `DELETE /temporary-chats/{id}` | Cancel work and discard context; idempotent `204`. No history/list/review endpoint exists for temporary chats. |
| `GET /admin/conversations`, `GET /admin/conversations/{id}/turns` | Separate audited saved-message review, with bounded user/date filters and current business grants. No impersonation, mutation, or automatic import into admin agent context. |

Turn input: `{request_id: UUID, expected_version: nonnegative integer, message: nonempty string}`. Owner/mode are server-bound. Reject supplied history, summaries, evidence, tool results, or model settings. Mode changes start separate chats. Current grants protect all reviewed/history content, including sensitive user text; omit items that cannot be safely filtered.

## 5. Completion, Retry, and Temporary Lifecycle

Serialize turns per chat. Resolve duplicate identity after current access checks **before** rejecting an old expected version: a matching committed retry returns its checked result without another model call. Same ID with changed content/version returns `409 idempotency_conflict`; a different request with stale version returns `409 version_conflict`. An in-flight duplicate returns `409 request_in_progress` with bounded retry guidance.

Saved completion atomically commits the complete turn and session version/state using the approved transaction/fence. No automatic SDK persistence/provider continuation. Pre-commit interruption discards working content; uncertain commit requires lookup/reconciliation. Report `not_committed` only when confirmed, otherwise `unknown`; neither is saved success. Preserve retry ID/body; rerun only confirmed failed/uncommitted attempts. Reload newer versions; never silently rebase.

Recheck session/permissions before commit/release; even committed results can be withheld after revocation. Temporary success advances memory only. End/logout/expiry/restart invalidates context; late responses cannot revive it. Idle expiry: **30 minutes**; cleanup sweep: **60 seconds** maximum. Enforce expiry before reuse. Persist no temporary IDs, hashes, contents, or retry state; logs omit temporary URL IDs and diagnostics use independent IDs. Unknown, expired, and cross-user temporary IDs get the same `410` after login checks; ending is idempotent.

## 6. Approved Tool Contract

Internal typed tools accept enabled metric/list operations, canonical site/filter IDs, period, approved grouping/sort/fields, and cursor. Server supplies authorization; verified mappings support "my leads."

Return authorized facts/rows, metric/policy versions, source/date provenance, scope, coverage, timezone/absolute boundaries, and retrieval time. Trusted code constructs queries/joins and calculations. OperationalReadRepository exposes no mutations, raw drivers/commands, arbitrary operators, server JavaScript, `$out`, or `$merge`. Credentials stay unchanged: protection is application-enforced and cannot guarantee safety if code is bypassed or credentials compromised.

Use the approved calendar rules: last seven days excludes today; today starts at local midnight; explicit rolling periods use elapsed hours. Require a verified site timezone; clarify cross-site ambiguity. Missing dates/mappings never become invented values. `converted_leads` and unapproved delivery/contractor tools remain unavailable. Retrieved free text cannot change instructions or grants.

## 7. Structured Answer Contract

The turn result contains `session_id`, `mode`, new `version`, `persistence:committed/not_saved`, and this validated `response`:

| Field | Meaning |
|---|---|
| `status` | `complete/partial/clarification/unsupported/unavailable`; valid zero is `complete`, not missing data. Transport/source failures use section 9 instead. |
| `answer`, `claims[]` | Plain text plus numerical-claim references to verified `fact_ids`; deterministic templates for simple metrics. Unsupported numerical claims cannot pass validation. |
| `facts[]` | `{fact_id, metric_id, metric_version, value:number|null, unit, evidence_ids[]}`. Null/unavailable values never become zero. |
| `datasets[]`, `visuals[]` | Dataset `{dataset_id, columns:[{key,label,type,unit?}], rows, next_cursor?}`; visual `{kind:card/bar/line/table, dataset_id, field_bindings}`. Text/visuals share facts/data; no executable code or arbitrary configuration. |
| `sources[]`, `evidence[]`, `coverage`, `warnings[]` | Safe source IDs/labels; evidence IDs link metric facts to source IDs/provenance. Known/unknown eligible/excluded coverage and partial/suppressed reasons; no restricted identifiers, secrets, or forbidden-scope counts. |
| `period`, `scope`, `retrieved_at`, `policy_version` | Period `{start_local,end_local,start_utc,end_utc,timezone,label}` uses inclusive start/exclusive end; typed applied filters, UTC freshness timestamp, integer authorization version. Null period for non-date questions. |

Clarification/unsupported replies may be complete saved turns without visuals. Missing evidence is unavailable/partial, never an unsupported total. Required-source failures are errors; approved independent partial answers disclose missing coverage. Withhold unsafe small groups until rules exist. Native tracing stays explicitly configured, developer-only, sensitive capture off; no trace endpoints. Temporary privacy distinguishes unsaved transcripts from safe provider diagnostics; `store=false` is separate.

Dataset continuation uses a new "next page" turn; the server loads this chat's validated cursor from question state. Recheck expiry/policy. Claims/evidence references must resolve inside this response; source locators stay server-side. Empty arrays/nulls represent inapplicable fields consistently.

## 8. Starting Resource Limits

Configurable starting limits, not measured performance targets:

| Area | Starting bound |
|---|---|
| Message / list page | 4,000 characters; page default 25, maximum 100 items |
| One response | At most 100 record rows and 20 chart groups; paginate or explicitly disclose grouping limits, never truncate before calculating a total |
| Execution | 10-second operational query deadline; 60-second total turn deadline including bounded retries; maximum 8 tool calls |
| Model context / output | 12,000 / 2,000 tokens, further capped by the configured model's supported limits; no silent dropping of the current question or required evidence |
| Admission | One active turn per chat; 10 new turns per user per minute; reject before paid work when exceeded |

Bound queries/joins/projections before execution; cancel on deadline/disconnect where possible. Enforce Security's approved byte caps before parsing/commit; implementation remains unverified. Context budgets preserve complete tool pairs and authorized summaries. Count the full permitted eligible set within deadline, never just a page.

## 9. Safe Errors

| HTTP status | Stable examples and behavior |
|---|---|
| `401` | `authentication_required/session_expired`; reauthenticate. |
| `403` | `approval_required/access_denied`; no restricted existence/count disclosure. |
| `404` | `not_found`; same response for missing and another user's saved resource. |
| `409` | `version_conflict/idempotency_conflict/request_in_progress/policy_changed`; reload/reconcile, not automatic unsafe retry. |
| `410` | `temporary_unavailable`; start a new temporary chat. |
| `422` | `invalid_request`; safe field/code only, never echo raw input. |
| `429` | `rate_limited`; bounded `Retry-After`. |
| `502/503/504` | `model_failure/response_invalid`, `source_unavailable/authorization_unavailable/persistence_failed/cleanup_pending`, `deadline_exceeded`; no fallback business numbers. |

Commit-related errors add `commit_state:not_committed/unknown` and the original request ID. Do not report `not_committed` without proof. Policy-store failure releases no protected results. UI messages use common language; details stay sanitized even for administrators. Tracing failure alone never fails an otherwise valid request.

## 10. Review and Remaining Specifications

API Contracts and Security are approved. Security defines key/rotation/replay, byte caps, browser controls, and cleanup bounds. Exact framework integration, disclosure/differencing policies, provider settings, saved/audit retention, source-date rules, staff/site aliases, metric exclusions, and transaction capability remain open or unverified; do not guess them or block unrelated work.

[Tickets.md](../Tickets.md) remains the only implementation register. Existing TICKET-001/002/003/005/006/007/008/009/010 and verification tickets cover these contracts; all remain Backlog. Pydantic/OpenAPI generation and contract tests belong to authorized implementation, not this draft.

UI/UX, its libraries, and Security are approved; Test Strategy is the current authorized draft. Stop for its review before the next document. Do not change databases/configuration, install dependencies, or implement endpoints yet.
