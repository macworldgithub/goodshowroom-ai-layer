# TICKET-034: Role and Employee-Access API Technical Plan

Date: 2026-11-06

Status: Draft for review. Planning is authorized. Implementation has not started.

Task authority: [TICKET-034 in Tickets.md](../Tickets.md#ticket-034-expose-role-and-employee-access-apis).

This document plans one ticket. It does not change the ticket status, approve new
business rules, or authorize deployment. Items described as proposed are
implementation choices for review. They are not existing application behavior.

## 1. Outcome and scope

Expose the five approved FastAPI operations for role and employee-access
management. An API is the defined request and response interface between services.
Every operation must use a verified administrator identity and current management
permission. A role defines maximum permissions. A user's scope narrows that role.

| Method and path | Planned result | Existing mutation service |
| --- | --- | --- |
| `GET /v1/admin/users` | Safe, bounded user list with editable record versions | New read orchestration |
| `GET /v1/admin/roles` | Safe, bounded role list with editable record versions | New read orchestration |
| `POST /v1/admin/roles` | Create a role and return its committed version; HTTP 201 | `AccessManagement.create_role()` |
| `PATCH /v1/admin/roles/{id}` | Edit or deactivate a role; HTTP 200 | `AccessManagement.update_role()` |
| `PATCH /v1/admin/users/{id}/access` | Change or revoke an approved user's access; HTTP 200 | `AccessManagement.change_user()` |

The ticket does not add browser pages or a browser proxy. It does not add user
creation, Google account edits, password management, identity mapping, access
choices, settings, operational data queries, or role deletion. TICKET-035 owns
access choices and mappings. TICKET-034 must preserve TICKET-033 behavior.

## 2. Approved basis and verified starting state

Requirements come from [User Flows, sections 3 and 4](../docs/03-USER-FLOWS.md),
[API Contracts, sections 1 to 3 and 8 to 9](../docs/07-API-CONTRACTS.md),
[Schema, sections 3 to 5](../docs/06-DATABASE-SCHEMA.md),
[Security, SEC-02/03/04/08/09/13/14](../docs/09-SECURITY.md),
[Test Strategy](../docs/10-TEST-STRATEGY.md), and
[the current decisions](../docs/DECISIONS.md).

- TICKET-026, TICKET-030, and TICKET-057 are Done. They are the registered
  dependencies. TICKET-033 is also Done and supplies reusable administrator code.
- TICKET-031 remains In Review. Real administrator site IDs and grants remain
  deferred. Use synthetic scopes for this ticket. Do not define missing site IDs.
- `AccessManagement` already checks management authority, target scope, record
  versions, policy versions, self changes, required audit, and the last active
  administrator. Extend only where the new transport needs a missing guarantee.
- `change_user()` accepts only an already approved target. It accepts new status
  `approved` or `revoked`. Do not add pending-user approval or revoked-user
  reactivation through this route. Access-request decisions remain TICKET-033.
- `update_role()` rejects edits to the caller's own role. It rejects a role
  reduction that would leave an approved user's scope outside the active role.
  Preserve these rules. Deactivation makes that role unavailable for protected
  work; it does not silently rewrite all assigned users.
- `access_management_users()` reads at most 1,001 rows to detect a population
  above 1,000. It also applies a 5-second database limit and a 1 MiB document
  budget. It is an internal safety reader, not a public list API.
- There is no corresponding bounded role population reader. There are no
  TICKET-034 routes or public role/user management contracts yet.
- `AdminAccess.authorize()` verifies the signed method, path, query, body,
  login ID, and generation before loading current `manage_access` authority.
  `AdminAccess.change()` is specific to TICKET-033 POST operations. It cannot
  be used unchanged for PATCH.
- `AdminScopeGrant` already uses encrypted selected-record handles. Raw
  `SourceReference` objects must stay internal. The current handle issuer only
  accepts records in the caller's own selected scope. This is insufficient for
  serializing every otherwise manageable role beneath an `all_permitted` grant.
- Administrator rate limits and safe error/response headers already exist.
- The current public OpenAPI export contains schemas and an empty `paths`
  object. Preserve that export convention in this ticket. Document the route
  contracts in the implementation README instead of claiming full operation docs.

The related leads, delivery, and sales-floor repositories were inspected for
role/site context. Their role names and site flags are not AI-layer authority.
Do not copy their token rules or treat a legacy `admin` label as a wildcard grant.
Their runtime APIs are not dependencies of these management operations.

The working tree contains uncommitted TICKET-033 code and other local files.
Record that baseline before implementation. Preserve existing `AGENTS.md`,
`frontend/next-env.d.ts`, environment files, and administrator discovery material.
Do not use a broad reset, cleanup, or unreviewed staging operation.

## 3. Public contract to implement

Use the existing envelope: `{schema_version: 1, request_id, data}`. The server
creates the diagnostic request UUID and uses the same UUID for the required audit
record. Set `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and
`Referrer-Policy: no-referrer` through existing response helpers.

### 3.1 Input rules

Use strict Pydantic JSON parsing through the existing public contract parser.
Reject extra fields, duplicate JSON keys, wrong types, unsafe integers, invalid
UUIDs, unknown capabilities, duplicate selectors, malformed handles, and excessive
depth or array size. Apply the existing 64 KiB body limit before parsing. Reject
bodies on GET. Reject queries on mutations.

IDs, creation dates, update dates, stored versions, and audit actor fields are
server-owned. Do not accept `provider`, `provider_account_id`, email changes,
login state, arbitrary query objects, or internal storage documents in a mutation.
UUIDs in paths must use the application's canonical form.

Proposed request models:

| Model | Required fields | Important rule |
| --- | --- | --- |
| `AdminRoleCreateRequest` | `name_key`, `active`, `capabilities`, `grants`, `expected_policy_version` | Server creates UUID, timestamps, and version 0 |
| `AdminRoleUpdateRequest` | `name_key`, `active`, `capabilities`, `grants`, `expected_version`, `expected_policy_version` | Path chooses the role; server preserves its creation date and increments its version |
| `AdminUserAccessUpdateRequest` | `approval_status`, `role_id`, `scope_limit`, `expected_version`, `expected_policy_version` | `approved` requires role and scope; `revoked` requires null role and empty scope |

Use the existing `name_key` and capability constraints. The capability allowlist
is `chat`, `review_saved_chats`, `manage_access`, and `manage_settings`. These
capabilities do not automatically grant business records.

PATCH will carry the complete editable access configuration, rather than a
generic patch document. All listed fields remain explicit. This avoids treating
an omitted field as a request to clear or preserve a security setting. The
operation still changes only the approved access fields. Add no JSON Patch,
Mongo update operators, or arbitrary nested merge behavior.

Use `AdminScopeGrant` for grants and scope limits. After verifying the caller,
resolve selected-record handles to internal `Grant` records. Then apply all
existing role and caller-scope checks. A valid handle is a selector, not authority.

### 3.2 List requests and pagination

Proposed queries:

- Both lists: `page_size`, default 25, maximum 100; optional `cursor`.
- Users: optional `approval_status` from the existing four statuses. Default
  `approved`, since this ticket's edit operation manages approved employees.
- Roles: optional `active` with exact values `true` or `false`. An omitted value
  includes manageable active and inactive roles.

Reject duplicate query keys, unknown keys, blank supplied values, and arbitrary
search/filter/sort fields. Use fixed ascending canonical UUID order. No offset
pagination, arbitrary sort, free-text search, or total count is needed.

Each page returns `{items, next_cursor, policy_version}`. Encrypt and authenticate
the cursor with the existing configured codec. Bind it to a distinct purpose,
caller UUID, resource kind, filters, page size, last returned UUID, and policy
version. Preserve the existing 300-second maximum lifetime and 4,096-character
token bound. Missing cursor configuration must deny the list safely.

For this bounded first implementation, reuse the internal user population reader
and add a matching role reader. Each reader must detect more than 1,000 records,
enforce its byte/time limit, and fail without a partial public page. Do not use a
100-row public page to decide whether another administrator exists.

Within a consistent policy snapshot, validate the population, determine which
rows the caller may manage, sort those authorized rows, and then page them.
The cursor position must come from an authorized returned row. Do not derive it
from a hidden row or disclose totals for hidden records. Determine continuation
from the authorized set, rather than from the number of hidden rows scanned.
This design trades repeated bounded internal reads for simpler privacy rules.
Retain the 1,000-record capacity limit until a separate scalability change is
reviewed. Never silently truncate the population to make a check pass.

### 3.3 Safe output models

Proposed public fields:

| Output | Fields |
| --- | --- |
| `AdminRoleItem` | `id`, `name_key`, `active`, `version`, sorted `capabilities`, tokenized `grants` |
| `AdminUserItem` | `id`, normalized `email`, nullable `display_name`, `approval_status`, `version`, nullable `role_id`, tokenized `scope_limit` |
| Role mutation data | `id`, committed record `version`, committed `policy_version` |
| User access mutation data | `id`, `approval_status`, committed record `version`, committed `policy_version` |

Create explicit page and mutation envelopes. Do not serialize `UserRecord` or
`RoleRecord` directly. Never return provider account IDs, login records, key
metadata, raw source references, password/token fields, or operational records.
Treat stored names and emails as untrusted text. No HTML rendering is added here.

## 4. Authorization and visibility rules

Use one shared management-authority predicate for mutation checks and list
visibility. Reuse the current narrowing rule: each grant must fit one permitted
parent grant. Do not union separate parent grants to manufacture broader access.

1. Verify the signed request and current login before any target lookup.
2. Require the caller's current approved account, active role, and `manage_access`.
3. Apply the authenticated administrator rate limit before list reads or writes.
4. A manageable role must have capabilities contained in the caller's role and
   grants contained in the caller's effective scope. Check the stored role as
   well as the proposed replacement. Narrowing an unmanageable role is denied.
5. An approved target user is manageable only when its current role capabilities
   and stored scope fit the caller's authority. A replacement must fit both the
   selected active role and the caller's effective scope.
6. For pending/rejected/revoked list entries, expose only safe identity/status
   fields and version. Return null role and empty scope. These entries do not
   become editable or reactivatable through TICKET-034.
7. The caller's own user or role may be visible if permitted. Existing services
   must still reject self access changes and edits to the caller's own role.
8. Missing or out-of-scope targets must use the same safe unavailable response
   after caller authorization. Only a visible target may produce a stale-version
   conflict. Do not return hidden roles, names, grants, or assignment counts in
   error details. Storage corruption fails closed without raw document output.
9. Recheck current identity and authority before response release. Deny a result
   if authority is lost while a list or mutation is running.

Selected-record output needs a small shared helper. It must first authorize the
complete stored grant beneath current caller authority. It can then issue
encrypted handles for that already-authorized grant's selected references. Bind
those handles to caller, current policy, purpose, and expiry. Keep direct source
references inside the backend. Do not relax the current issuer into a public
arbitrary-source-token service. No operational record search or new public handle
issuance endpoint belongs in this ticket.

## 5. Mutation and transaction sequence

A transaction commits related database writes together. The existing policy
fence is a shared write guard that makes competing access changes conflict.
The required audit must commit in the same transaction as the access change.

For each mutation:

1. Verify the exact signed method, canonical path, empty query, and raw body.
   PATCH must be verified as PATCH. Do not verify a reserialized JSON body.
2. Check current management authority and rate admission. Parse the strict body.
3. Check target visibility, both supplied versions where required, selected
   handles, capability subsets, and scope narrowing.
4. Build storage records from trusted current records plus allowlisted editable
   values. Generate IDs and times on the server. Preserve identity binding and
   creation dates. Sort public capability arrays for stable output.
5. Invoke `create_role()`, `update_role()`, or `change_user()`. Keep business
   mutation rules inside `AccessManagement`; do not copy them into HTTP handlers.
6. Inside the transaction, advance the shared fence using the expected policy
   version. Re-read caller, role, login, and target. Check the expected target
   version at the write, rather than relying only on an earlier read.
7. For role edits or user revocation, evaluate the complete bounded user
   population and the proposed final state. Preserve at least one approved user
   with an active role, valid narrowing scope, and `manage_access`. Do not replace
   this rule with a count of role labels or currently logged-in sessions.
8. Write the record, advance policy once for the mutation batch, and append the
   required audit under the same fence. Recheck authority before commit.
9. Commit, then recheck caller authority and safe response visibility. Return
   record and policy versions from a confirmed committed state. Withhold a
   response if a competing edit or revocation prevents safe readback.

The current management service uses `db.transaction()`, whose transaction options
are defaulted. The repository already has bounded snapshot/majority transaction
support for protected work. During implementation, verify the actual management
transaction settings. Use the same snapshot read concern, majority write concern,
and 5-second commit bound through a narrow management transaction method if
needed. Do not use a saved-chat capability to authorize management writes. Test
the resulting options and rerun TICKET-026/033 regressions if this shared path
changes.

### Role-specific behavior

- Create starts at version 0. Unique role-name conflicts return a safe conflict.
  Do not claim a new create is idempotent after a lost response.
- Edit increments the target role version by one. Deactivation is `active:false`
  through the edit route. No DELETE route is added.
- Reject an active-role grant reduction that would invalidate an assigned
  approved user's stored scope. Do not silently narrow or revoke those users.
- Check last-administrator safety for both role capability removal and role
  deactivation, including simultaneous changes by two administrators.

### User-specific behavior

- Preserve provider binding, verified email, display name, and creation date.
- Require both `expected_version` and `expected_policy_version`.
- Only already approved users may be changed by the current service.
- Revocation clears the role and scope. It must make later protected actions
  fail through current policy checks. Do not claim this route destroys a Google
  session or physically clears all process memory.

### Failure and retry rules

Use the existing safe catalog: 401 for failed authentication, 403 for insufficient
management authority, a uniform 404 for unavailable targets, 409 for visible stale
versions/policy, 422 for invalid input, 429 for admission limits, and the existing
503/504 failures for unavailable storage or deadlines. Never echo raw input or a
database exception. Preserve existing middleware classifications where applicable.

The repository has `AppCommitOutcomeUnknown`. An unknown commit can have written
data. Map it to a safe failure with `commit_state:unknown`; do not call it rollback
or report success. Also treat loss of confirmation after commit as uncertain from
the caller's perspective. Do not automatically replay a mutation after such a
failure. Reload authorized lists to inspect the current record and policy versions.
Do not add an unrelated durable retry ledger or recovery endpoint.

## 6. Planned code structure and file changes

These are planned changes, not files already implemented for TICKET-034.

| File | Planned purpose |
| --- | --- |
| New `src/goodshowroom_ai/admin_management.py` | Coordinate verified identity, safe management lists/projection, handle conversion, existing mutation services, and result readback |
| New `src/goodshowroom_ai/admin_management_routes.py` | Five fixed handlers; strict query/body/path parsing; admission; safe envelopes |
| `src/goodshowroom_ai/admin_access.py` | Reuse verification and add only the narrow shared scope/handle methods needed by both tickets; preserve TICKET-033 dispatch |
| `src/goodshowroom_ai/access_management.py` | Share management-authority predicates; expose confirmed versions where needed; preserve self and last-admin rules |
| `src/goodshowroom_ai/storage/mongo.py` | Add the bounded internal role reader and any narrowly required management transaction/result-version support |
| `src/goodshowroom_ai/api_contracts.py` | Add role/user input, item, page, and mutation models; register them in `PUBLIC_MODELS`; enforce request-byte limits |
| `src/goodshowroom_ai/cursors.py` | Add purpose-specific typed wrappers if needed; reuse current encryption, expiry, and rejection behavior |
| `src/goodshowroom_ai/access_workflow.py` | Add a factory for the management service using the existing database/verifier/authorization/clock |
| `src/goodshowroom_ai/app.py` | Wire service startup/injection and the five explicit routes |
| `src/goodshowroom_ai/api_http.py` | Only if needed for an explicit safe unknown-commit classification; avoid changing unrelated error behavior |
| `src/goodshowroom_ai/export_contracts.py` | Update the declared schema stage; keep one source for generated schema definitions |
| `frontend/scripts/generate-contracts.mjs` | Register the new exported validators |
| `frontend/src/lib/api-contracts.ts` | Add the new public types, byte limits, HTTP-status and semantic checks |
| `contracts/openapi.json` | Regenerated backend contract |
| `frontend/src/lib/generated/api-types.ts` | Regenerated public TypeScript types |
| `frontend/src/lib/generated/validators.cjs` | Regenerated runtime validators |
| `frontend/src/lib/generated/validators.d.cts` | Regenerated validator declarations |
| `frontend/src/lib/generated/contract-meta.json` | Regenerate only if its source metadata changes; do not force an unrelated diff |
| New `tests/test_admin_management.py` | Signed route, projection, version, and failure tests |
| `tests/test_access_management.py` | Extend service safety tests only where the ticket adds or repairs behavior |
| New `tests/test_admin_management_storage.py` | Bounded population, projection inputs, ordering, and transaction-option tests |
| `tests/test_api_contracts.py`, `tests/test_cursors.py`, `tests/test_admin_access.py`, `tests/test_admission.py` | Add relevant contract/handle/cursor/admission regressions where changed |
| `frontend/src/lib/api-contracts.test.tsx` | Valid and hostile input/output fixtures for the new public formats |
| New `tests/isolated_role_management_mongo.py` | Explicit guarded local MongoDB rollback, version-race, and last-admin checks |
| New `tests/isolated_role_management_http.py` | Loopback HTTP checks using synthetic state; cover POST and PATCH signed-byte transport |
| `README.md`, `Tickets.md`, `planning/PROGRESS.md` | Record usage, actual commands/results, limits, and review state after implementation |

Prefer these narrow additions over a generic administrator CRUD framework. A
CRUD framework is a common layer for creating, reading, updating, and deleting
records. This ticket needs only the five approved operations. No dependency,
operational-schema, credential, frontend page, or lockfile change is planned.

## 7. Execution order and completion checks

### Step 1: Pin the implementation baseline

Re-read instructions, ticket, and this plan. Confirm dependencies are Done.
Capture current Git status and exact diff, including untracked TICKET-033 files.
Record the starting revision. Once implementation is authorized, mark TICKET-034
In Progress. Do not treat a public page, a Git push, or an old test count as a new
verification result.

Complete when: implementation scope, baseline, and required shared helpers are
identified without changing existing local work.

### Step 2: Define and test public formats

Add strict role/user requests, item projections, page envelopes, and mutation
results. Review full PATCH semantics and visibility rules before handler code.
Add JSON fixtures for valid edits, revocation, hostile fields, versions, and
selected handles. Regenerate backend/frontend artifacts from the same models.

Complete when: parsers accept valid intended inputs, reject prohibited inputs,
and backend/frontend representations agree. No storage record is exported.

### Step 3: Add bounded lists and protected handles

Add the missing role reader. Reuse the user population bound. Apply shared
authority predicates before projection and pagination. Add separate cursor
purposes for user and role pages. Recheck identity and policy before release.
Serialize selected grants through the verified-grant handle helper.

Complete when: list results, page metadata, and selected handles cannot disclose
out-of-scope entries or bypass authority; size/time/capacity failures are safe.

### Step 4: Wire the three mutation routes

Support POST and PATCH through exact method/path/query/body verification. Build
records only from server-held state and allowlisted edits. Invoke the current
management services. Check both versions, preserve audit identity, confirm commit,
and validate output. Keep TICKET-033 routes working.

Complete when: allow/deny/version tests pass, failed audits leave no changed
record, and neither self edits nor a caller's own role edit can escalate access.

### Step 5: Prove transaction and administrator safety

Run against a newly verified disposable loopback MongoDB replica set. Check
process configuration, exact data directory, replica-set identity, fixed app
database, empty/owned test state, and permitted network targets before writes.
Refuse a nonempty unowned target. Keep default offline tests network-blocked.

Use synthetic identities, generated test keys, and explicit ownership markers.
Do not use real Google accounts, production grants, or operational databases.
Remove only records owned by the run, verify cleanup, and stop only the server
started for that run. Preserve any pre-existing test data. Make setup repeatable
without patching module globals or requiring a historical temporary directory.

Complete when: real rollback and race results meet section 8, with exact outputs
recorded. A simulated race alone is insufficient for this completion claim.

### Step 6: Final verification and review handoff

After edits are complete, run the focused checks, required full regression checks,
contract generation checks, and isolated runs on the final code state. Review the
final diff for exposed storage fields, overbroad target selection, skipped guards,
and unrelated changes. Record failed/repaired attempts and remaining limits.

Complete when: acceptance evidence is written, every changed file is explained,
and the ticket is In Review. Mark Done only after required user review. Publication
and deployment remain separate actions from planning or local verification.

## 8. Required test matrix

| Area | Cases and expected result |
| --- | --- |
| Signed identity | Missing/duplicate header, forged or expired token, wrong purpose/method/path/query/body, stale login generation, revoked login: deny before data release or mutation |
| Current permission | Pending/rejected/revoked caller, inactive caller role, no `manage_access`, permission lost during awaited work: deny |
| Strict request | Extra identity fields, raw source objects, arbitrary operators, invalid UUIDs, bool/float/negative/oversized versions, duplicate keys, unknown capabilities, oversize/deep body: safe rejection |
| Lists | Allowed user/role, hidden role/user, safe unapproved projection, own read with self-write denial, inactive target role, unknown/duplicate filters, empty pages, first/last page, fixed order: correct bounded result |
| Cursor and handle | Changed actor, resource kind, filter, page size, policy, expiry, purpose or token bytes: deny; never accept a raw source reference |
| Population bounds | 1,000 and 1,001 records; document/aggregate-byte bound; database timeout; missing or malformed referenced role: safe bounded behavior, no truncated safety check |
| Role create/edit | Valid create/version 0; valid edit/version increment; duplicate name; old record/policy version; immutable field injection; own-role edit; capabilities or grants beyond authority: correct success or safe denial |
| Assigned users | Role grant reduction that invalidates one approved user's scope: rollback; safe change retains valid narrowing |
| User access | Valid role/scope change and revocation; target outside authority; self change; missing target; stale versions; pending/rejected/revoked reactivation: deny unsupported transitions |
| Last administrator | Direct last-admin removal, role deactivation, and removal of `manage_access`: deny without changed record or audit |
| Simultaneous edits | Two writes with one record/policy version: at most one commits; target version advances once; one success audit; losing request fails safely |
| Competing administrators | Two admins revoke each other or deactivate each other's management roles: at least one effective administrator remains in the committed database |
| Audit and storage faults | Required audit insert failure, write failure, final authorization failure, and readback failure: no false success; known rollback leaves records and versions unchanged |
| Unknown outcome | Inject an uncertain commit result or lost confirmation: safe unknown result, no automatic replay or false rollback claim |
| Admission/response | Ten-per-minute current admin limit, separate own-request window, rate recovery, no-store headers, bounded output, catalog-only errors, matching audit/request UUID |
| Regression | Existing TICKET-026/032/033 behavior, provider binding, private token/key exclusion, and generated contract consistency stay intact |

Use a barrier after both requests read the same starting policy/version and
before either request acquires the shared write fence. Do not wait at a barrier
while one transaction holds that fence; this can deadlock the test. Use bounded
test deadlines. This makes a concurrency test exercise a real conflict instead
of two sequential calls. Inspect committed users, roles, policy/fence, and audits
after the race. Count effective administrators from valid current role/scope
records; a role label or a successful HTTP response is not sufficient evidence.

For loopback HTTP verification, start only owned listeners on verified free
ports. Send exact signed POST/PATCH bytes to the running backend. Check success,
stale edits, changed body, and denial over an actual socket. A network check with
a database substitute proves transport only. Label it separately from real
MongoDB verification. No browser automation is required for these backend routes.

## 9. Verification commands

These are commands to run during implementation. They were not run for this plan.
New test modules below are planned names, not files that exist yet.

From `goodshowroom-ai-layer/`:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_admin_management.py tests/test_admin_management_storage.py tests/test_access_management.py tests/test_admin_access.py tests/test_own_access.py tests/test_api_contracts.py tests/test_cursors.py tests/test_admission.py
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check --no-cache .
.venv/Scripts/python.exe -m ruff format --no-cache --check .
.venv/Scripts/python.exe -m goodshowroom_ai.export_contracts --check
git diff --check
```

After implementing and verifying each isolated harness target:

```powershell
.venv/Scripts/python.exe -m tests.isolated_role_management_mongo
.venv/Scripts/python.exe -m tests.isolated_role_management_http
```

From `goodshowroom-ai-layer/frontend/`:

```powershell
npm run test:unit
npm run typecheck
npm run lint
npm run contracts:check
```

Regenerate intentionally before the final checks when schemas change:

```powershell
# Repository root
.venv/Scripts/python.exe -m goodshowroom_ai.export_contracts
# Then, from frontend/
npm run contracts:generate
```

Check current Postman setup with `postman status`. Reuse a matching local
collection if one exists at implementation time. The existing schema-only
OpenAPI file cannot be assumed to generate runnable requests for these routes.
Do not initialize a workspace, publish contracts, or claim a Postman run merely
to satisfy a planning step. A local HTTPX/isolated-harness run remains valid
evidence when accurately labeled. Run `postman status` at final verification and
report its actual state.

## 10. Acceptance mapping and completion decision

| Ticket criterion | Required implementation | Required evidence |
| --- | --- | --- |
| Typed grants, current management authority, both versions | Strict wire models, verified identity, shared authority checks, transactional policy and target-version comparison | Positive/negative routes, hostile input, stale versions and permission-loss tests |
| No cross-scope changes, self escalation, or concurrent last-admin loss | Existing narrowed services, full bounded safety population, shared fence, last-admin check on proposed state | Service tests plus real MongoDB simultaneous role/user and last-admin races |
| Only authorized output and safe errors; audit failures deny writes | Explicit projections, authorized-set pagination, protected handles, atomic required audit and safe unknown-commit treatment | Disclosure sentinels, cursor/handle tests, rollback and unknown-outcome tests, public contract checks |

Implementation is ready for review only when all five operations and their
applicable tests are complete. Record actual counts and commands rather than
reusing TICKET-033's historical 984/79 results. Keep criteria unchecked when
required evidence is missing. State any capacity, single-process, provider,
transport, browser, or production limit that remains.

The immediate next step is review of this technical plan. After implementation
is authorized, begin with the strict public contract tests in step 2 and complete
the ticket through its evidence and review handoff. TICKET-035 is the next
dependency-eligible access API ticket after this work; it needs its own
authorization.
