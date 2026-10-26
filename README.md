# GoodShowRoom AI Layer

Purpose: Provide the project entry point and link to approved project guidance.

Status: Local packages initialized; implementation continues through Tickets.md.

## Local frontend

Use Node.js 24 and npm. From this repository folder:

```powershell
cd frontend
$env:NEXT_TELEMETRY_DISABLED = '1'
npm ci
npm run dev
```

Open http://127.0.0.1:3000. Expect a light page titled **Good Showroom AI**
and a message that login and chat will be added later. Resize the window to
check that the card and text fit. No login or chat controls exist in this ticket.
Stop the server with Ctrl+C.

Run the checks from `frontend/`:

```powershell
npm run lint
npm run build
npm run typecheck
npm run check:setup
npm start
```

Lint and typecheck must exit without errors. Build must complete. With
`npm start`, the same page must load at the local URL. Run build before the
first typecheck so Next.js can generate route types. The frontend needs no
running backend for this setup page.

`check:setup` builds with a random non-secret environment marker, scans browser
assets and rendered HTML for it and configured private values, checks root
environment loading, starts a temporary local server, and checks unavailable
API/environment-file paths. It stops that server and verifies `.env` is unchanged.
It uses no browser automation or live business services.

Lint compatibility limitation: Next.js 16.4.2's import, React, and accessibility
plugins still require ESLint 9. ESLint 9.39.5 is pinned with strict peer checks;
ESLint 10 was rejected rather than forced. ESLint 9 is end-of-life. Review a
compatible maintained lint stack when upstream plugins support it. This tool
limitation does not grant production readiness.

The TICKET-018 npm audit reported five high-severity entries from one
development-only chain: Next's lint plugin → fast-glob → micromatch → braces.
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
has no published patched version. The checked production dependency audit has
zero findings. Use only trusted local lint patterns; do not expose lint/glob
tools to chat or API inputs. Do not use audit's suggested Next.js 14 downgrade
or force an upgrade. This unresolved tooling finding requires review.

Installation verification on this machine needed a workaround after npm stalled:
the Next.js archive was downloaded from npm, checked against the lockfile's
SHA-512 integrity, and extracted into generated dependencies. `npm rebuild
--ignore-scripts --offline` repaired executable links. The full installed tree
and lockfile checks passed afterward. A fresh uninterrupted `npm ci` has not
been verified on this machine. If it stalls, preserve the lockfile and report
the error; do not bypass integrity or peer checks.

The frontend `.npmrc` requires exact direct versions and valid peers. It also
disables dependency lifecycle scripts. Review any package that needs such a
script before changing this policy; ordinary project scripts still run.

`next.config.mjs` loads the existing repository `.env` on the server using
Node's environment-file loader. Existing process variables take precedence.
The file is optional for this shell. Private settings are never exported with
Next.js `env` or `NEXT_PUBLIC_*`; keep credentials out of public variables.
No frontend-local environment file or copied credential is needed.
This is a local setup shell. Authentication, request protections, and business
screens remain with their registered implementation tickets.

## Local backend

Use Python 3.13 and uv. From this repository folder:

```powershell
uv sync --locked
uv run --locked python -m goodshowroom_ai
uv run --locked ruff check .
uv run --locked ruff format --check .
```

The backend listens on `127.0.0.1:8000` in one process. Stop it with Ctrl+C.
It loads the repository `.env` explicitly. `APP_ENV` accepts development, test,
or production; it defaults to development. Invalid configuration stops startup
with a fixed message. No business routes or API documentation routes exist yet.
The default app does not open a database or model connection. Request access
logging is disabled. PyMongo is installed for the app-only storage foundation;
no database is created or configured by this setup command.

## App-only Mongo storage foundation

TICKET-021 pins PyMongo 4.19.0. App storage reads only `AI_LAYER_MONGO_URI` and
selects the fixed `byd_ai_layer` database. It does not accept an operational
database name. `create_app()` does not connect by default. A caller must opt in
to the app lifecycle connection. Index creation is a separate explicit call
and owns only the two `audit_events` indexes in this ticket. No import or normal
startup path creates indexes.

Protected access-control writes use a typed, deny-by-default internal context.
The fence update and required audit insert share one transaction session. BSON
documents use aware dates and a one MiB encoded-size limit. Offline fake-client
tests check target selection, denied access, cleanup, conflicts, audit failures,
and validation. They do not prove MongoDB rollback or server transaction/index
support. Those checks remain pending until a verified isolated target is
available. This internal context is not the future `AuthorizationService`.

## Backend test harness

TICKET-019 adds development-only Pytest, pytest-asyncio, HTTPX, and cryptography.
From the repository root, run:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest --strict-markers -m "not integration and not live"
```

Expect 56 passing tests and no skips. Plain `uv run --locked pytest`
uses the same offline marker selection. Unknown markers/config options and
warnings fail the run. The 25 TICKET-021 storage tests use fake clients and do
not prove real Mongo transactions or indexes. These tests do not prove chatbot
features.

`tests/conftest.py` installs guards before test-module collection. It removes
application/provider environment settings, disables environment-file access and
tracing flags, and denies DNS/TCP/UDP calls. Windows' standard-library internal
socket pair is allowed only within that call for the async event loop. In-memory
HTTPX ASGI requests need no external socket. Test environments, app state,
dependency overrides, and session guards are restored after use.

Fixtures provide a controllable clock/race gate, deny-by-default mock policy,
scripted model/repository/exporter outcomes, and fresh in-memory 3072-bit RSA
keys. Set explicit mock outcomes for each case. No fixture proves production
authorization, Mongo transactions, real model quality, or provider privacy.

The database guard requires a reviewed literal loopback endpoint, three exact
run-owned namespaces, and matching ownership markers from a read-only reader.
A name ending in `_test` is insufficient. Recheck ownership before cleanup.
Synthetic marker readers verify guard logic only. No database is provisioned.

The approved external selectors are reserved:

```powershell
uv run --locked pytest --strict-markers -m "integration and not live"
uv run --locked pytest --strict-markers -m live
```

There are no real integration/live cases yet: these commands deselect the
harness tests and return the no-tests-selected code. This is pending evidence,
not a passed integration run. Selected external cases require `--allow-test-db`
or `--allow-live-model`; even those flags fail closed until reviewed adapters
exist. They do not enable networking. Self-checks verify these failures in
separate pytest processes. No credentials or Atlas setup are needed for offline
work.

These are Python-level regression guards for reviewed tests, not an operating
system sandbox. Trusted test plugins load before the guards. Native code or a
separately launched unguarded program can bypass Python patches. Do not run
untrusted test code or use real credentials in fixtures. Future external
adapters need explicit scoped network/target controls in their owning tickets.

## Frontend test harness

TICKET-020 adds component and synthetic browser checks. Run these commands from
`frontend/` with the locked development dependencies installed:

```powershell
npm run test:unit
npm run test:harness
npm run lint
npm run typecheck
npm run test:e2e
```

The browser runner also needs Playwright's Chromium binary. If it is missing,
run `npx --no-install playwright install chromium` as a separate setup step.
That setup step downloads the browser; ordinary tests use synthetic local data.

Expect one component test and six browser cases to pass. `test:harness` checks
network rules, safe reporter callbacks, diagnostic-file suppression, and denied
command overrides. Its failure-report checks simulate reporter callbacks; they
do not run a deliberately failing browser test.

`test:unit` runs once and exits. `test:e2e` uses isolated browser contexts and a
server made only for tests at `127.0.0.1:4178`. Keep that port free. The harness
uses invented identity/API fixtures;
these checks do not verify Google login, real authorization, chat persistence,
or the later application journeys. No running FastAPI service, database, or
provider key is needed. The production setup page is not a completed chatbot.

Launchers remove application/provider environment variables and inherited Node
options. Vitest does not load environment files. Browser fixtures allow only
listed local GET routes, reject redirects and direct API request contexts,
block WebSockets, and close each context. Node guards deny external TCP, DNS,
and fetch requests. Use the npm launchers; direct runner/config overrides deny.
`npm run test:e2e -- --list` lists safe discovery counts without launching a browser.

Screenshots, videos, traces, snapshots, raw error details, and response-body
logging are disabled. Safe reporters print fixed labels, outcomes, and counts.
A preload blocks Playwright's content-bearing `error-context.md` file writes.
These controls cover reviewed tests, not hostile code or every native network
path. They are not an operating-system sandbox. Do not use real data or keys.

Playwright MCP could not create an isolated browser context on this machine
(`Target.createBrowserContext: Not allowed`). The terminal Playwright runner
passed all six cases instead. The MCP helper remains unverified; no shared
browser context was used as a workaround. These tests do not inspect the product
website. Real journeys remain with TICKET-054/055.

Playwright global setup starts the server inside the runner. Teardown closes
its connections and listener. The final browser run exited successfully and
released port 4178. No standalone server command is needed.

TICKET-020 dependency review: full `npm audit` reports **8 development-tree
findings: 2 moderate and 6 high**. Five high entries are the earlier lint chain.
The added test tools contribute `@vitest/mocker`/`vitest` moderate entries and a
Vite high entry. The registry did not contain the patched versions suggested by
npm during this check. The production-only audit reports **zero findings**.
Fixed one-shot commands, no exposed runner UI, synthetic data, and network
guards reduce exposure; they do not fix vulnerable packages. These new findings
need user review under TICKET-020. Do not force dependency overrides or downgrade
the application to satisfy audit suggestions.

## Chatbot user and role storage

TICKET-022 adds private UserRecord/RoleRecord models and named methods on the
app transaction interface. New users are pending without grants. Profile edits
cannot change provider identity or access. Role and profile replacements use
expected versions. Unique index definitions cover provider identity, email, and
role name; index setup is explicit through initialize_identity_indexes().

Writes require a typed internal capability, the policy fence, and a required
audit in the same transaction. The policy version advances once per identity
transaction. These interfaces are for trusted identity/policy services. They
are not HTTP endpoints or complete authorization/login workflows. Future
services must check active roles, user scopes, and their permitted intersection.
No source accounts or passwords are imported. No live indexes were created.

Run the standard offline Pytest and Ruff commands above. The new fake-client
suite checks model round-trips, denied access, duplicate conflicts, stale edits,
policy updates and storage bounds. Actual MongoDB unique indexes, concurrent
writers and rollback remain unverified; no safe isolated test target was used.

## Windows test-folder permissions

Default pytest runs disable the optional result cache and use a new temporary
folder for each process. This avoids folders owned by a different Windows
account. Strict warnings and offline safety guards remain enabled. Old cache
and pytest-of-* folders are left untouched. Do not widen their permissions.

Run each command separately from this repository root. The default pytest
selection already excludes integration and live tests:

```powershell
uv run --locked pytest
```

Do not append `cd` or another command to the pytest line. A combined paste such
as `pytest ... cd C:\...` makes pytest treat `cd` as a test path.

## Access-request and email-preapproval storage

TICKET-023 adds a strict combined access-request record and private transaction
methods. Requests bind to an existing pending user and that user's email.
Preapprovals store explicit scope within an active role without a placeholder
user. Availability checks use a supplied aware clock value and reject expiry
at the exact boundary. Pending duplicates recover the existing row; a concurrent
unique-key conflict requires a fresh transaction. Closed/expired resubmission
remains unavailable until a policy is approved.

Explicit index setup defines partial uniqueness on (kind, email_key) for active
records and a status/creation index. No index setup runs automatically. Named
read/list/create/close methods require internal capabilities. Writes require
the shared fence and audit. Lists have bounded rows, data size and cursor time.
Only rejection/revocation storage is exposed here. TICKET-025/026 own approval
and consumption workflows; these methods do not grant user access. There are
no new HTTP routes or screens. Real MongoDB partial uniqueness, races and rollback
remain pending; ordinary tests use synthetic offline data.

## Login validity storage

TICKET-024 adds private login records and owner-bound read/exchange/renew/revoke
methods. The record stores only UUIDs, generations, versions and UTC validity
metadata. It never stores cookies, Google/internal tokens or chat content.
Original authentication start/expiry cap the absolute eight-hour lifetime.
Leases cannot exceed five minutes. Expired/revoked bindings cannot be renewed;
exchange replay returns the existing valid binding without extending it.
Revocation is idempotent for the same generation and retains expiry/replay data.

Writes require typed capabilities, the existing fence and required audit.
Index setup is explicit. The TTL cleanup field is `purge_after`, distinct from
access expiry, to retain replay evidence through assertion expiry/tolerance.
See the visible TICKET-024 review refinement in docs/06-DATABASE-SCHEMA.md.
No live indexes or database were changed. Signed verification and temporary
memory cleanup remain future tickets. Run the default offline Pytest/Ruff
commands; mocks do not prove real transaction conflicts or TTL behavior.

## Signed internal identity boundary

TICKET-001 adds a server-only Next.js signer and a backend verifier. The wire
profile uses RS256 with exactly 3072-bit RSA keys, a fixed issuer/audience,
allowlisted key IDs and separate request/bootstrap claims. Assertions last at
most 120 seconds with 30 seconds of verification tolerance. Login/lease expiry
has no tolerance. Normal assertions contain user/login UUIDs and generation;
bootstrap contains verified provider identity and original auth timing only.
Neither profile accepts role/grant claims.

Operation binding includes the uppercase method, a strict normalized path,
and SHA-256 of four-byte big-endian query length + exact raw query bytes + exact
body bytes. Query excludes the '?' prefix. Forward identical bytes; do not
re-encode JSON/query data between signing and verification. Ambiguous paths,
extra fields/headers, token-directed keys and oversized assertions deny.

Keys/configuration and clocks are injected. No real keys were generated or
added to .env. Signing stops at 90 days; planned retired verification keys have
210 seconds of overlap, and revoked keys have no grace. Keep the live registry
current and reverify/reload before protected commit/release. The verifier uses
a trusted loader for current persisted LoginRecord metadata and checks binding,
generation, revocation and exact validity. Loader failures deny. The bootstrap
identity-to-user/approval transaction remains TICKET-025; jti alone is not replay
prevention. TICKET-024 owns durable exchange binding.

No Google callback, public login route or generic proxy is added here.
TICKET-031 owns callback/transport and TICKET-056 owns browser CSRF/proxy checks.
Future transport must keep assertions/keys server-only, use safe fixed errors,
and never return claims/tokens to browser payloads or logs. The server-only
import guard prevents accidental client imports of the signer.

API references checked: [PyJWT](https://pyjwt.readthedocs.io/en/latest/api.html)
and [jose SignJWT](https://github.com/panva/jose/blob/main/docs/jwt/sign/classes/SignJWT.md).

## Staff mappings and typed settings

TICKET-027 adds private identity mappings and named sites/retention/disclosure
settings. Mapping records require a source-qualified staff identifier, app user,
verified actor/time and explicit canonical sites. Name/email-only bindings reject.
Mappings default inactive. The repository checks parent existence and verifier
identity; actual source evidence must be verified by the trusted service before
it constructs a mapping. No live staff record was inspected by this ticket.

Settings use fixed named documents. Unknown fields, raw query/metric definitions,
ambiguous aliases and unsupported timezones reject. Missing timezone/retention
values remain unset. Disclosure is disabled until a full policy is approved;
no cohort threshold is invented. Numeric retention fields do not install TTL
indexes or automatically delete content. tzdata 2026.5 supplies Windows timezone
data; it adds no new service.

Private methods require explicit settings capabilities, expected record versions,
the existing policy fence and required audit. Mapping identity/user bindings are
immutable; updates advance mapping version as well as record version. Explicit
index setup defines source-qualified uniqueness and user/active lookup. No live
index setup or migration was run. TICKET-035 owns public admin validation and
mutation routes. TICKET-027 is approved with its recorded limits. The earlier
approved snapshot was pushed to GitHub; subsequent work remains local.


## Central authorization

TICKET-002 adds the internal `AuthorizationService`. RBAC means the role sets
maximum permissions. ABAC means user, site, staff assignment, record, field and
chat attributes restrict those permissions. Missing approval, an inactive role,
an invalid login, an unavailable policy or an unknown collection denies access.
An individual scope must fit one role grant. Separate grants cannot be combined
to obtain a wider set of fields or tools.

Roles have explicit `chat`, `review_saved_chats`, `manage_access` and
`manage_settings` capabilities. The default is empty, including for existing
role records. Role names grant nothing. A review capability does not grant
business data or developer traces. Capability assignment and administrator
workflows remain their owning tickets; no role is automatically upgraded here.

`AppDatabase.load_authorization_policy` reads the current user, role, login,
policy version, sites and at most 100 active mappings in one MongoDB snapshot
transaction. Unsupported transactions and malformed records deny. No policy
cache is used. Operational reads require an explicit trusted `CollectionPolicy`
allowlist; its default is empty. This ticket enables no live business tool.

The service returns typed query constraints. Repository callers must apply
site and record restrictions before querying or counting. An assigned-record
constraint uses verified, active, source-qualified staff mappings. A selected
constraint retains complete source references. Each joined collection needs a
separate `DataAccess` check before lookup. Its `fields` must include all fields
used in selection, projection, sorting, grouping and join predicates. Trusted
adapters supply record attributes; model output is never proof of ownership.
Returned records and protected history evidence must be checked with those
attributes before release. Unknown attributes deny or omit the affected item.
Sensitive breakdowns remain disabled until TICKET-006 supplies an approved
complete disclosure policy. No numeric threshold is invented.

Consumers obtain `Principal` only from verified internal assertions. The
required `check_identity` dependency must recheck the live signing-key registry
and assertion/login binding from TICKET-001, rather than trust browser claims.
It is called before and after the policy load. `recheck(decision)` reloads current
policy and denies any changed snapshot, including role/grant/mapping/login
changes without a policy-version increment. A lease renewal can conservatively
invalidate a running decision. Call it at tool, history, commit and release
boundaries. Saved writes also compare `decision.policy_version` through the
existing transaction fence; a service check alone does not make writes atomic.
Revocation applies at those boundaries; it cannot recall delivered information.

Owned chat actions check the owner. Temporary actions also bind the login;
temporary history and administrator review are denied. Saved review returns
`requires_review_audit=true`: the review consumer must write its required audit
and filter all transcript/evidence content under current business grants before
release. Authorization objects contain private policy state. Never serialize
these objects to client responses, logs or traces. The service itself writes no
logs, transcripts or temporary state.

HTTP, operational repository, history and review routes remain future tickets.
They must wire this service and its constraints at each protected boundary.
Offline tests can prove module behavior; they do not prove real MongoDB snapshot
isolation, concurrent transaction fences, signing-key lifecycle integration or
an end-to-end authenticated route. No database/index setup occurs on import.


The shared async transaction wrapper now awaits `start_transaction()` before
entering its context. This matches the installed PyMongo 4.19.0 API and the
[official async session API](https://pymongo.readthedocs.io/en/latest/api/pymongo/asynchronous/client_session.html).
Earlier synchronous test substitutes hid this driver incompatibility. The
substitutes now follow the real async contract. This correction does not prove
real database transaction support or rollback behavior.

Security review for this module: role escalation is limited by whole-grant
containment; record/field/site/collection escapes require explicit constraints;
stale decisions fail on policy/login rechecks; temporary access binds user and
login; admin review requires its own capability, audit and content checks.
The principal, policy loader, collection registry and record attributes are
trusted server dependencies. Allowing clients or model output to construct them,
or ignoring a returned constraint, would bypass these module safeguards. Future
transport/repository tickets must preserve that boundary and test its wiring.


## Operational read repository

TICKET-003 adds `OperationalReadRepository` with three named methods:
`list_records`, `count_records`, and `list_joined_records`. Callers supply a typed
`ReadRequest`, not MongoDB JSON. It accepts exact-value and UTC date-range filters.
The repository constructs the query, projects approved fields, and applies current
site/selected-record/verified-assignment restrictions before reading or counting.
Every field used for output, filtering, ownership or a join must pass authorization.
A count requires an explicitly permitted metric. The count pipeline has no page
limit; it counts the full authorized match within the query deadline. Metric
meaning, date provenance and coverage remain TICKET-004/005 responsibilities.

Trusted `CollectionBinding` records define source paths, scalar types, canonical
site values, identity types and qualified staff references. `JoinBinding` records
allow specific same-database relationships only. Both sides are authorized before
lookup; the right-side pipeline applies its own record scope and projection.
Returned records are checked again before release. Unknown binding, malformed
value, unsupported mapping or stale policy returns a safe failure. Credential
fields cannot be included in field bindings. Retrieved notes remain untrusted
business data and never become application instructions.

There are no production binding defaults. Source normalization must be verified
under TICKET-004 before enabling a live adapter. The explicit connection factory
selects only `byd-leads-new` and `byd-panel`, requires TLS certificate/hostname
validation and does not create indexes. It does not connect on import or load
`.env`; future startup code supplies the existing secret URI values. App writers
continue to use their separate `byd_ai_layer` repository. This ticket changes no
source schema, index, credential or environment value.

All generated stages/operators/field paths and lookup targets pass a recursive
allowlist before driver execution. There is no public command, mutation, raw
client/collection, query or pipeline method. Write stages, JavaScript, arbitrary
regex, union targets, unknown expressions and nested lookup escapes are rejected.
Only the current explicitly authorized joined target can reach the validator.

Queries use a 10-second server limit and an application deadline covering the
read and its authorization checks. Disk spilling is disabled. Explicit binary
collation prevents inherited source collation from changing identifier/site
matching. Identity/site/assignment type guards exclude unverifiable security
attributes before aggregation. Pages use ascending Mongo `_id` with a bounded
page-plus-one query; pages default to 25 and cannot exceed 100 rows. Joined output
has a combined 100-record cap; overflow fails instead of silently losing records.
Small cursor batches and a 1 MiB query/result cap bound materialization. Scalar
text over 4096 characters is unavailable, not silently truncated. Cursors close
on success, failure and cancellation; source failures never become zero counts.

`PagePosition` is an internal continuation, bound to the user, policy version,
exact typed request and source identity. Never expose or accept it directly at an
HTTP route. TICKET-057 owns encrypted expiring transport cursors. Pagination is
not a frozen source-data snapshot; concurrent source changes can change later
pages. Only the left side of a joined read has pagination; right-side matches
must fit the combined result cap. No cross-database join is enabled.

Security review covered query/operator injection, unapproved collection/field
access, cross-site or assigned-record leaks, joined-record escapes, stale policy,
source value types, large results and timeout cleanup. Typed requests, trusted
bindings, recursive checks, current authorization and bounded cursor draining
protect these boundaries. Private handles and internal contracts are application
boundaries, not protection against compromised Python code or stolen credentials.
Current MongoDB credentials remain unchanged and are not read-only credentials.

The driver call shape was checked against the installed async driver and
[official PyMongo aggregation guidance](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/aggregation/).
Offline command spies verify the module. Real isolated MongoDB reads, server
query behavior/plans, large-source performance, production bindings and HTTP
integration still require separate evidence. No live operational command or
mutation-denial probe was run.


## Source normalization and reporting periods

TICKET-004 adds `source_dates.py` and `normalization.py`. The lead normalizer reads
through OperationalReadRepository, checks current policy, and releases only typed
metadata from authorized rows. It checks sites, source identity and normalized
staff references again before release. It does not write source records or infer
permissions. Duplicate primary references fail instead of silently deduplicating.

`build_lead_adapter(settings)` builds the observed `byd-leads-new.leads` field
map for trusted startup. It uses BSON `_id`, explicit site aliases and the known
platform/status/stage/date/source-ID fields. It reads no customer or staff names, phone numbers,
passwords or customer notes. `dealer` is the query site; populated `dealerName`
must resolve to the same configured site. A secondary site label is not an
implicit fallback for missing primary scope. Missing/conflicting site mappings
stay unavailable. This factory does not connect or register a live tool.

Source-date fields can use the new `source_date` read type. It preserves bounded
strings, datetimes and nulls for normalization. It does not permit a direct date
range query or establish received-date provenance. Strict `date` fields retain
their earlier behavior. Unknown formats, missing timestamps, date-only precision,
unverified zones and ambiguous/nonexistent local times produce explicit states.
ISO offsets and aware BSON dates convert to UTC. Local wall times require a
reviewed source timezone; neither the machine zone nor the dealership zone is
silently assumed for a source timestamp.

A parsed timestamp is a candidate, not proof of when a lead was received.
`DateEvidence.received_at` is available only for a valid timestamp under a trusted
original/native DateRule with a review reference. It stays null for unverified,
imported, scraped or test-drive provenance. These rules are code-owned review
attestations, never model/source-record claims or admin-editable metric settings.
The built adapter has no positive provenance rule. No live source's original
creation time was certified by this ticket.

This distinction follows the reference import code. In
`byd-leads-backend/seed/seed-virtualyard.js`, leadCreatedDate can use scrapedAt;
test-drive-only records can use testDriveDate. `seed-both.js` has equivalent
fallbacks. The normalizer never falls back to leadDate, createdAt, receivedDaysAgo,
scrape time or a test-drive date when a received timestamp is absent/unverified.
The inspected Scope figures remain historic discovery evidence, not test data or
current reporting totals.

Reporting periods require a configured site timezone. Last seven days means the
seven completed local days before today. Today starts at local midnight and ends
at the injected current time. Rolling hours use elapsed UTC time. Boundaries use
inclusive start/exclusive end and handle daylight saving changes. Ambiguous or
nonexistent boundaries are unavailable rather than shifted. Cross-site timezone
selection remains an open business decision. Python's
[zoneinfo](https://docs.python.org/3.13/library/zoneinfo.html) and
[datetime](https://docs.python.org/3.13/library/datetime.html) guidance informed the
round-trip checks. The existing tzdata dependency supplies Windows zone data.

The known platform set includes dealer_studio. Status and stage stay independent;
sold/delivered is not translated into converted, and pending is not invented.
Source references preserve database, collection, key field and string/ObjectId
type. Equal-looking IDs do not merge entities or prove relationships. Extra
source IDs are aliases only, never a deduplication or permission rule.

The default adapter has no staff mapping. A custom StaffColumn must represent a
reviewed stable-ID field and match an active source-qualified user mapping for the
current user/site. Display names, emails and phone numbers are not evidence of
ownership. Unknown ownership stays unverified and cannot expand assigned access.

Relationship evidence supports the four observed panel links: opportunity to
customer, delivery and sales log; customer to delivery. It matches only explicit
typed references among already-authorized target rows. Unresolved means absent
from those authorized results, not proof that a record does not exist. Duplicate
matches are ambiguous. The helper issues no query; callers must recheck current
permissions before releasing its evidence. A matched link proves neither a sale
nor a converted lead.

Coverage counts describe the returned authorized page only. They distinguish
verified received dates, unavailable/missing dates, unknown platforms and
unverified ownership. They are never whole-source totals. TICKET-005 owns metric
meaning, eligibility, exclusions, deduplication and full coverage queries. Live
source provenance, exact staff/site configuration, sparse relationship edge cases
and query performance remain unverified. No source schema/index/credential,
environment value or approved product requirement was changed.

Security review covered guessed dates, timezone ambiguity, fuzzy site/owner
matching, ID coercion, stale permissions and relationship existence leaks. Fixed
source maps, typed references, explicit provenance, current authorization and
restricted evidence guard these paths. Helpers consume already-authorized data;
they do not grant access or replace the protected read boundary.


## MetricRegistry and Q-01 lead counts

TICKET-005 adds a code-owned, versioned MetricRegistry and MetricService.
`total_leads` implements Q-01 only: eligible leads received in the previous seven
completed days for one permitted dealership. The registry records its source,
identity, exclusions, date reliability/representation, timezone, filters,
aggregation, coverage and authorization rules. An entry is not execution-ready
until identity and exclusion reviews are explicit. The default live definition
has no such attestations, so it returns unsupported without querying data.
`converted_leads` is disabled and cannot be enabled through this definition.
Q-02 through Q-05 remain their own tickets; question/request data cannot enable
them or change business rules.

The first implemented identity option counts distinct Mongo source documents
using the unique `_id`. It does not assume that two imported records describe
different real-world leads. Its use needs an explicit identity review. Source
inclusions and exclusions also need review; a review of no additional exclusions
must be deliberate. The default registry supplies neither review. Actual
cross-source deduplication and more complex exclusion rules are not invented.
Positive review references in tests identify synthetic policies only.

The operational repository now also has the named `count_date_cohort` read.
Its source is fixed to `byd-leads-new.leads`, raw `_id`, `platform` and
`leadCreatedDate`. Only the fixed count_leads tool and total_leads metric can
use this method. The Q-01 contract accepts site/record permissions only; it
rejects caller date overrides, extra filters and alternative metric semantics.
Authorization checks the tool, metric, site, fields and selected/assigned scope
before execution and again before release.

A single MongoDB operation applies the authorized record scope, projects the
three necessary source fields and calculates five bounded counters using a
restricted `$facet`: candidate records, eligible records, explicitly excluded
records, reliable dates and reliable dates inside the period. It has no page
prelimit and never adds page lengths. Known eligibility and reliable dates require
scalar source codes; reliable dates also require reviewed original/native
provenance and BSON date representation. Strings, missing dates and unverified
origins remain unknown for this count, even when a separate normalizer can parse
a string. Unknown source codes are reported as unknown eligibility, not silently
excluded. Excluded records and coverage counts come only from the authorized
scope. No customer rows or identifiers are returned by this operation.

The private validator accepts only these five facet names, each with an optional
match and a final count. It rejects limits, lookups, nested facets, JavaScript and
write stages inside them. All earlier query time, materialization, collation,
fixed-target and application read-only controls remain in effect. See the
[official MongoDB facet documentation](https://www.mongodb.com/docs/manual/reference/operator/aggregation/facet/)
and [aggregation type checks](https://www.mongodb.com/docs/manual/reference/operator/aggregation/type/).
One operation avoids combining changing pages; this is not proof of server
snapshot isolation under concurrent source writes.

MetricResult separates exact `value` from `verified_count` and records site,
metric/policy versions, period/timezone, retrieval time, source and coverage:

- Complete: all permitted eligibility/dates are known; value is the exact count,
  including a verified zero. An empty eligible set can be a verified zero under
  reviewed identity/exclusion rules.
- Partial: some reliable records exist but other eligibility/dates are unknown.
  value is null. verified_count is the known subset in the period, not a total.
- Unavailable: there is no usable verified subset or the site timezone is missing.
  No exact count is returned. Source/driver failures raise a safe error, never zero.
- Unsupported: the metric/family is disabled or identity/exclusion rules are not
  reviewed. No source query runs.

Coverage applies to the full authorized query rather than one page. Typed result
validation checks the coverage partitions, count bounds and absence of an exact
number in partial/unavailable/unsupported states. Calculations use backend integer
arithmetic and server counts, not a model. BSON Int64 results become Python ints;
malformed, fractional, negative or inconsistent counters fail safely.

These are internal metric facts. TICKET-006 still owns disclosure safeguards and
TICKET-007 owns evidence/response construction. Do not serve these objects directly
to employees, expose raw coverage as a breakdown, create visuals, or bypass final
permission/disclosure checks. No model tool, API, screen or provider integration
was enabled here. Live identity/exclusion reviews, original-date provenance,
source/query performance and real MongoDB execution remain unverified. Synthetic
fixture success is not approval of those live rules.

Security review covered unreviewed metric activation, source/date/namespace
substitution, false zero, page truncation, hidden unknown records, stale grants,
malformed counters and nested pipeline escapes. Fixed typed definitions, current
scope checks, restricted aggregation and explicit uncertain states protect the
module boundaries. Registry definitions are trusted implementation configuration;
neither administrators nor source/model text can supply them as runtime policy.

## Aggregate disclosure boundary (TICKET-006)

`disclosure.DisclosureService` sits after metrics and before the future
ResponseBuilder (TICKET-007). It defaults to `status=withheld, aggregate=null`.
No disclosure policy is approved for live data. Default withholding checks current
metric/tool/field/site/record authorization without fetching counts. Existing
authorization continues to deny sensitive breakdowns.

A threshold alone cannot prevent totals, complementary groups, overlapping filters,
or repeated queries from revealing a small group. The only supported opt-in is a
trusted, reviewed fixed publication: one complete Q-01 cell per metric/site, with
an explicit positive minimum group and exact evidence pinned, including period,
coverage, policy version and retrieval timestamp. Synthetic fixtures are not
business approval. Review must cover the whole publication family, prior versions
and external totals; replacing a publication needs that review again. This is a
conservative testable seam, not a general live analytics policy engine.

Only full-site `all_permitted` grants can qualify. Selected/assigned subsets,
changed evidence, alternate periods, partial/unknown counts and below-threshold
values remain withheld. There is no breakdown/filter/export channel or automatic
broader-total fallback. Repeating the same publication cannot disclose a new
value; changed source evidence or timestamp fails the exact match. The boundary
keeps no query ledger, writes no Temporary Chat state, and gains no new permission
after restart. Broader live/differencing rules remain deferred until an approved
policy can justify them.

Released facts contain only the permitted count, source, site, versions,
period/timezone and retrieval timestamp. They omit internal coverage counters,
verified subsets, warnings and suppression details. Future text/table/chart/export
consumers must use this same payload and never raw `MetricResult`. ResponseBuilder
and HTTP release still require their own authorization rechecks. This boundary
rechecks current policy before returning.

Security review: the attack surfaces are unapproved metric/group requests,
assignment or selected-record subsets, complementary/overlapping releases,
changing totals over time, coverage-counter leaks, and permission revocation.
Current grants, no grouping/filter interface, exact reviewed evidence matching,
restricted output projection and final authorization checks protect this supported
boundary. Cross-version/external-release review is a human policy requirement,
not a guarantee enforced by a historical query ledger. No source query, model,
HTTP response or visual integration is enabled merely by defining a publication.

## Evidence-backed response construction (TICKET-007)

`responses.ResponseBuilder` builds one complete, non-streaming Q-01 response from
`DisclosureService`, never raw `MetricResult`. It first checks the trusted chat
owner/login binding plus current tool/metric/site/field grants. A released value
must match the requested site, metric, period family and current policy version;
only full-site grants qualify. The builder rechecks authorization before return.
The future chat repository must resolve the actual owned session before creating
`ResponseContext`; the request body must never supply this trusted object.

The typed response has one count fact, one source/evidence link and one dataset.
Its card and table reference that same dataset. The deterministic answer is
`Total leads: <verified value>.` Its numerical claim references the same fact.
Safe source labels, metric/policy versions, site scope, absolute calendar dates,
timezone and retrieval timestamp accompany the permitted result. Cohort coverage
counts and raw source record locators stay internal. Coverage is a safe status.

All candidate model prose is replaced, including text with apparently matching
numbers. A few matching digits cannot prove a statement or its implied arithmetic.
The first response slice therefore has no free model Markdown, HTML, links, code,
chart configuration or computed visual values. Cross-field validation rejects
changed claims, facts, evidence references, dataset values or visual bindings.
Periods must represent seven completed local calendar days with matching UTC and
IANA-timezone boundaries. The final JSON is revalidated and capped at 1 MiB.

The content-free notice path distinguishes `partial`, `unavailable`, `unsupported`
and `clarification`, with fixed safe messages and no facts or visuals. Disclosure
withholding is `unavailable` with `disclosure_withheld`. Only trusted orchestration
chooses a notice; it must not translate a required-source failure into missing
data. `AccessDenied`, `SourceUnavailable` and sanitized `ResponseInvalid` remain
distinct errors for the future API adapter. Error messages and exception text do
not enter the success response.

The schema supports a complete verified zero without conflating it with missing
data. The approved disclosure boundary still refuses zero/partial/live count
release: this ticket adds no policy exemption. Synthetic approved positive
publications exercise the full response path. Content-free partial notices can
be built, but partial numerical answers need an approved disclosure extension.
Q-02–Q-05, categorical/trend charts, record tables, HTTP envelopes/headers,
session storage and frontend rendering remain with their registered tickets.
This module makes no model calls and persists no saved or temporary chat content.

Security review covered model number/markup injection, dangling evidence links,
text/visual disagreement, raw coverage leaks, scope substitution, stale policy,
timezone mismatch and output size. Closed typed graphs, deterministic text,
disclosure-only input, minimal safe metadata, size checks and current authorization
protect this supported path. Typed validation alone is not authorization; later
commit, history and HTTP release must recheck their own current permissions.

## Shared API contracts and safe errors (TICKET-030)

`api_contracts.py` defines public create-chat/turn requests, the completed-turn
envelope and the error envelope. It reuses `StructuredResponse`; it exports no
internal identity assertion, repository, driver or trace shape. The success
envelope is `{schema_version:1, request_id, data}`. Errors use
`{schema_version:1, request_id, error:{code,message,retryable,commit_state?}}`.
Saved mode requires `persistence=committed`; temporary mode requires `not_saved`.
These fields do not prove that a commit occurred: the future route must first
verify completion, authorization and current release access.

Use `parse_contract(Model, raw_bytes)` at the wire boundary, rather than relying
on direct model construction alone. It rejects unknown fields, invalid UTF-8,
duplicate keys, nonfinite/fractional/exponent numbers, noncanonical UUID syntax,
missing wire defaults and invalid UTC/RFC3339 timestamps. All current wire numbers
are integers within JavaScript's safe range. UUIDs have the hyphenated form; UTC
timestamps serialize as UTC, while local period timestamps retain their offset.
The current structural bounds are depth 20 and 20,000 nodes, in addition to
64 KiB request and 1 MiB response caps. Wire output includes explicit defaults,
nulls and empty arrays; only inapplicable error `commit_state` can be omitted.
Future decimal metrics need an explicit compatible contract extension.

`api_http.py` installs fixed error handlers in the existing route-free FastAPI
factory. The catalog covers the approved authentication/access/conflict/temporary/
validation/rate/source/model/response/persistence/deadline outcomes, plus ordinary
405/413/500 failures. No raw exception, validation input, body, path, trace or
caller header is copied into error output. Error IDs are generated by the server;
a future fixed route can bind a validated body request ID to private request state.
Only explicitly commit-related errors accept a verified `not_committed/unknown`
state; generic handlers never assume a commit failed. Responses use `no-store`,
`nosniff` and `no-referrer`. `success_response` validates the complete outgoing
envelope. No business route or authentication bypass is added.

A small ASGI failure boundary also stops unexpected exception messages from being
re-thrown to ordinary server logging before a response starts. After a response
starts it raises only a fixed sanitized transmission error and sends no second
body. Cancellation is not converted to success. TICKET-057 adds a 65,536-byte ASGI
request cap before route parsing. It returns the fixed 413 error before calling
FastAPI when a body is too large. The middleware reads at most the allowed body
size and replays it to FastAPI; it does not include the body in errors.

## Turn limits and cursors

TICKET-057 adds one-process turn admission. A chat can have one active turn, and a
user can start up to 10 turns in 60 seconds. The backend does not wait on a busy
chat. It returns a fixed `request_in_progress` or `rate_limited` error. `Retry-After`
is fixed at 1 second for a busy chat and 60 seconds for the user rate limit.
Admission metadata and locks stay in process memory and do not store chat text.
This needs one backend process; no Redis or queue is used.

The shared execution budget fixes the approved bounds: 60 seconds per turn, 8
tool calls, 12,000 context tokens, 2,000 output tokens, 100 result rows, and 20
chart groups. Exceeding a row or group bound fails the response; it never silently
truncates data or changes a total. No automatic retries are enabled yet. A future
caller must retry only a confirmed safe operation within the same turn deadline.

Operational page positions are sealed with Fernet using a separate key named
`AI_LAYER_CURSOR_ENCRYPTION_KEY`. Generate a new purpose-specific key with:

```powershell
.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the result in the ignored repository `.env`; never reuse the Next.js identity,
CSRF, or another application key. Do not paste it into chat or commit it. Cursor
tokens expire after 5 minutes. They bind the current user, query digest, fixed
ascending Mongo `_id` order, and policy version. The decoder rejects bad, expired,
cross-user, changed-filter, or stale-policy tokens before a position reaches the
read repository. The read repository still checks current authorization. If the
cursor key is absent, cursor operations must remain unavailable. Existing business
routes are not implemented by this ticket. Fernet keeps cursor claims encrypted
and authenticated, but its issuance timestamp is visible in the token. The source
position, caller, policy version, and query digest remain encrypted.

`contracts/openapi.json` is a deterministic offline OpenAPI 3.1 component export
with empty paths. Live docs remain disabled. `frontend/scripts/generate-contracts.mjs`
generates TypeScript types, static validators, their declarations and a shared error/
limit catalog. `frontend/src/lib/api-contracts.ts` accepts raw JSON, applies those
validators, and checks the same fact/evidence/visual, mode, timestamp and error
invariants as Python. Pass the actual HTTP status when decoding a transport result.
It does not expose validator diagnostics. Client validation never grants access.

Generation uses pinned `openapi-typescript` and Ajv with `ajv-formats`. Ajv
standalone output avoids runtime schema compilation and dynamic code evaluation;
only generated validators and their required helpers enter runtime imports.
Microsoft's `jsonc-parser` visits raw tokens to reject duplicate keys, comments,
bad number forms and deep input before decoding. Generated files are excluded from
manual lint rules and verified by regeneration; handwritten code remains linted.
See the official [OpenAPI TypeScript API](https://openapi-ts.dev/node),
[Ajv standalone pattern](https://ajv.js.org/standalone.html), and
[Microsoft JSON parser](https://github.com/microsoft/node-jsonc-parser).

Regenerate from the repository root, then the frontend directory:

```powershell
.venv\Scripts\python.exe -m goodshowroom_ai.export_contracts
cd frontend
node scripts/generate-contracts.mjs
```

Use `--check` on each command to detect stale generated artifacts without writing.
`contracts/fixtures.json` is shared synthetic test data for both languages. Schema
generation cannot express every Python cross-field validator, so both runtimes
also test semantic mutations against the same fixtures. Complete zero, partial
notices and safe errors stay distinct; accepting a fixture never authorizes its
publication or proves saved/temporary persistence. Live disclosure remains gated.

Security review covered raw error reflection, header/ID spoofing, duplicate-key
replacement, excessive nesting/size, number precision/type confusion, source/
claim/visual drift and unsafe runtime evaluation. Fixed catalogs, bounded strict
parsers, server-owned IDs, generated schemas, semantic checks and static validators
guard this public boundary. Source/role/session authorization, real commits,
browser transport defenses and end-to-end rendering still need their ticket owners.

## Saved-turn persistence

TICKET-008 adds the app-only `ChatRepository` commit boundary. It accepts a
validated `TurnCommand` and `BuiltResponse`; it does not accept SDK intermediate
items or run model/source work. A saved turn, its parent version and follow-up
state, the access-control fence, and a fixed audit event commit together in
`byd_ai_layer`. A changed request payload or stale session version conflicts.
Current owner, login, role, and policy checks run before replay/commit and before
the result is released. Attempt metadata stores only opaque IDs, a request hash,
status, times, and a fixed error category. Unknown commit outcomes are reconciled
by reading the owner-scoped turn; unresolved outcomes remain blocked and never
rerun model work automatically.

Create the app-only unique turn indexes explicitly with
`AppDatabase.initialize_turn_indexes()`. No index setup runs at app startup.
Operational databases and their credentials are unchanged. Read-only access to
them remains an application-code safeguard, not a database credential guarantee.

The offline saved-turn tests use the real application repository/models with a
rollback-aware fake database. They check atomic rollback, retry/recovery, access
revocation, version conflicts, payload changes, size/expiry linkage, and audit
metadata. They do not prove real MongoDB transaction or unique-index behavior.
The focused saved-turn suite passed 24 tests; related storage/admission tests
passed 86 tests; the full offline suite passed 666 tests. Ruff check, formatting,
Python compilation, and `git diff --check` passed. The test harness has no
verified isolated MongoDB target, so that acceptance check remains open. No
connection URI value was used, and no operational or app database was contacted.

## Tracing configuration foundation

TICKET-011 pins `openai-agents==0.24.0` and `openai==3.36.1`. The application
factory configures a fresh SDK trace provider before any trace is created. It
replaces default processors and never constructs the default native exporter.
`app.state.tracing` owns the lifecycle. The SDK has process-wide tracing state;
configure it once at startup, never on each request. A replaced or closed runtime
returns disabled run settings.

`APP_ENV=development` and explicit `OPENAI_TRACING_ENABLED=true` permit diagnostic
creation. Missing/unknown environment, missing/invalid enablement, test, and
production environments disable tracing. Sensitive capture is always false,
including when `OPENAI_TRACING_INCLUDE_SENSITIVE_DATA=true` is supplied. The
example flags do not turn on external export. TICKET-012 provides the sanitizing
exporter described below; provider access/retention must be reviewed first.

`TracingRuntime.run_config("saved" | "temporary")` builds real SDK `RunConfig`
objects with matching disabled controls, static workflow names, fresh random
diagnostic IDs, `trace_include_sensitive_data=False`, and
`ModelSettings(store=False)`. The outer `root_trace` applies the same controls.
It accepts no customer, account, source, login, or chat identifiers. Model calls
must use this configuration when TICKET-009 implements agent execution. Request
stage spans and correlation wiring belong to TICKET-013.

Tests can inject an in-memory capture processor to check the actual SDK without
network calls. Diagnostic callback/flush/shutdown errors produce only the fixed
`tracing_diagnostic_failure` category and do not replace application errors.
No OpenAI API/tracing key is loaded by this module. No inspection route exists;
application administrator roles do not grant OpenAI developer dashboard access.

`store=False` controls supported model-response storage, separately from SDK
traces. Future enabled export can create provider-side diagnostic records,
including permitted metadata for Temporary Chat. It must contain no temporary
transcript. Actual provider access/retention and export behavior remain unverified.
The official [Agents SDK observability guide](https://developers.openai.com/api/docs/guides/agents/integrations-observability)
and the pinned Python SDK source define the controls checked for this ticket.

From this repository, verify the tracing foundation offline with:

```powershell
uv --cache-dir .uv-cache run --offline --locked pytest tests/test_tracing.py -q
uv --cache-dir .uv-cache run --offline --locked ruff check src tests
uv --cache-dir .uv-cache run --offline --locked ruff format --check src tests
```

The focused tracing suite passed 47 tests; the full suite passed 734 tests.
Package compatibility, lockfile, OpenAPI export and Python compilation checks
also passed. This proves local configuration behavior with in-memory SDK traces;
it does not prove live provider retention, developer dashboard access, or native
export sanitization. No browser review is needed for this ticket.

## Trace export filtering

TICKET-012 adds `trace_export.py`. One native SDK batch processor sends copied,
allowlisted records through `SanitizingExporter` to the native
`BackendSpanExporter`. It replaces the default processor path. The normal app
factory still supplies no native exporter. `configure_tracing(native_exporter=...)`
is a trusted server-startup seam for use after provider access/retention review;
it is not an administrator setting, HTTP argument, or export destination picker.
Offline tests inject the exact native exporter with an in-memory HTTP transport.
The capture-only test seam is mutually exclusive with native export.

The filter accepts static saved/temporary workflow names, SDK-formatted random
trace/span IDs, UUID4 diagnostic IDs, normalized UTC event times, fixed stage,
tool and outcome/error names, and bounded token/duration counters. It copies
only these fields. It removes unknown fields, all chat/customer/business data,
model names/configuration, tool input/output/MCP data, source/auth/chat IDs,
raw errors/stack traces, links, and per-trace credentials. Unknown span types,
unapproved names, malformed permitted fields, and unsafe batches are dropped.
Responses spans become usage-only generation spans, without response IDs.

Canonical names for later agent/stage tickets are `GoodShowroom.PrimaryAgent`,
the current `count_leads` tool, and `authorization`, `context_preparation`,
`argument_validation`, `tool_execution`, `data_validation`,
`response_construction`, `final_policy_check`, `persistence`, `memory`, and
`response_validation`. SDK task/turn spans keep usage only. Future tools or
span types require an explicit allowlist update and privacy tests.

Safe JSON snapshots are frozen before queueing, so later SDK-object mutations
cannot change queued records and raw transcript objects are not retained by
the diagnostic queue. The native queue has 256 entries and 32 items per batch;
each item is limited to 16 KiB and a batch to 512 KiB. Each native export has a
two-second deadline and one attempt. Flush/shutdown are bounded; shutdown drops
remaining diagnostics. Reconfiguration stops the old processor and drops its
queue. Already sent or in-flight provider records cannot be retracted.

SDK/HTTP transport logs emit fixed categories through a safe handler. Exception
objects, raw provider error bodies, and log extras are not forwarded. Filter,
serialization, worker, export, flush and close failures drop diagnostics; they
do not turn an authorization failure into success. Keys stay in server-owned
native authentication headers, not trace payloads or per-trace key overrides.
No alternative destination, infrastructure, or monitoring system was added.

The deadline/shutdown hooks adapt the installed Agents SDK 0.24.0. Tests must
recheck them before an SDK upgrade. Actual provider ingestion, dashboard access,
and retention remain unverified. Native export remains off in the app factory
until that review; Temporary Chat can then export permitted metadata only.

Verify the export filter offline from this repository:

```powershell
uv --cache-dir .uv-cache run --offline --locked pytest tests/test_trace_export.py tests/test_tracing.py -q
```

The filter suite has 68 tests, plus 47 tracing-configuration tests. HTTP capture
uses the real native SDK encoder with `httpx2.MockTransport`; no request leaves
the process and no real key is needed. No browser review is needed for this
backend change. Final full offline Pytest passed 802 tests; Ruff lint/format,
lockfile, API-schema export, Python compilation and Git whitespace checks passed.

## Primary agent execution

TICKET-009 adds `PrimaryAgentService`. It creates one `GoodShowroom.PrimaryAgent`
with the current permitted `count_leads` tool. Model selection is explicit;
startup must supply the configured model, with no new default selected here.
The model interprets Q-01 or returns a typed clarification/unsupported plan.
Backend code rechecks the selected site and uses the existing ResponseBuilder
for all numbers, evidence and visuals. Model prose and raw SDK intermediate
items never become a saved answer. Tool JSON rejects unknown/duplicate fields,
invalid site values and inputs over 1,024 bytes.

`ChatRepository.load_agent_history` reads the most recent 20 complete turns
from one owned, unexpired app snapshot and applies current grants to each turn.
The service filters history again before model use. It keeps whole recent
user/assistant pairs under a conservative UTF-8 byte ceiling and per-call token
checks. A small deterministic hint can preserve verified Q-01/site state when
older pairs are omitted. The hint is ephemeral data, not a trusted instruction
or stored prose summary. Failure to build a hint drops it. Follow-ups query
current data; they never reuse a historical count as a current result.

The SDK receives explicit input without `session`, `conversation_id`,
`previous_response_id` or automatic response chaining. Supported model requests
use `store=False`, sensitive tracing is off, and native trace export remains
gated as described above. Tool availability, site scope and protected result
release depend on AuthorizationService, independently of model instructions.
Stored text cannot select a new database, tool or permission scope.

Saved turns use existing admission, attempt/replay, atomic commit and recovery
services. Matching completed retries recheck ownership/data grants and skip
model work. Unknown commits remain unknown. Working items are discarded on
failure; only a validated completed turn reaches the commit boundary. Access is
checked again before commit and after persistence before releasing the result.

Temporary mode dispatches only to an injected memory store and validates its
owner/login/session/version binding. No default or persistent fallback exists.
TICKET-010 implements that store's expiry/logout/cancellation lifecycle below;
startup must inject it. HTTP routes and browser integration remain
with their registered later tickets. No database schema or source repo changed.

Run the focused offline checks from this repository:

```powershell
uv --cache-dir .uv-cache run --offline --locked pytest tests/test_agent_context.py tests/test_agent_service.py tests/test_agent_history_repository.py -q
```

These tests use synthetic policy/database dependencies and also run the actual
Agents SDK runner with a fake model. No provider key, network call or browser is
needed. They do not prove live model quality or real MongoDB transaction behavior.

Focused TICKET-009 tests passed 33. Final full offline Pytest passed 835 tests in
82.20 seconds; lint/format, lockfile, API-schema export, compilation and Git
whitespace checks passed. Luna medium executed final verification under the
latest user instruction; earlier test authoring used Luna max before that update.

## Temporary Chat lifecycle

`TemporaryMemoryStore` keeps conversation content, IDs, request hashes, retry
results and pending-task references in one process. It binds each chat to the
exact user, login UUID and generation. No MongoDB chat repository, filesystem,
browser persistent storage, Redis or durable SDK session is used. The app factory
starts its cleanup task and closes it only when a store is explicitly injected;
there is no unauthenticated default store.

Each protected entry, commit and release checks current authorization and login
validity. The store revalidates the existing five-minute login lease and
eight-hour absolute authentication limits. Context also expires after 30 minutes
without chat activity. A fresh verified login renewal may extend an unexpired
lease through `refresh_login`; it does not reset chat idle time or revive context
whose cached lease already expired. Login integration must call that hook after
successful verified renewal.

End, logout, expiry and shutdown discard owned context references and cancel
active tasks. Commit/release checks reject deleted or expired context even when
model code returns late or ignores cancellation. Restart has no recovery path.
Unknown IDs and IDs owned by another valid user/login produce the same `410`
after authentication. End is idempotent. In-memory replay checks the original
request hash before version checks; changed content cannot reuse an old request
ID. Cached replies can be replayed only after current permission checks.

The store bounds active chats (256 by default), retained turns (20), cached
replies (32) and distinct request identities (1,024 per context). Reply eviction
never removes request identity binding. An aged-out reply is not automatically
rerun; status can remain unknown. Reaching request capacity ends that context
with the same unavailable outcome so a new empty chat can be started. These
are memory limits, not durable retention or recovery guarantees.

The cleanup loop runs every 60 seconds while the process operates. If a cleanup
notification fails and there is no new verified lease renewal, reuse stops at
the existing lease expiry (at most five minutes); memory removal follows within
the next sweep. A paused process or failed sign-out does not prove immediate
remote deletion. The application must report unconfirmed cleanup/sign-out and
let the user retry. Discarding references is not a secure RAM-erasure promise.

`LogoutCoordinator` invalidates local temporary contexts before calling the
trusted backend login-revocation callback. The callback writes login validity
metadata only. `coordinateLogout` in the frontend clears browser working state,
attempts the protected backend revoke, and attempts NextAuth sign-out even when
revoke fails or hangs. Each callback has a ten-second default timeout; the revoke
callback receives an abort signal. It returns separate safe result flags and
fixed notices for local cleanup, backend cleanup and sign-out. No raw callback
error or chat content is logged. Concrete NextAuth/CSRF routes and screens stay
with TICKET-031/039/048; this ticket adds their tested coordination seams.

Offline verification uses injected clocks, real synthetic login/policy records,
task cancellation, memory restart and callback faults. No browser or live Google
sign-in was exercised. Existing real MongoDB and provider review gates remain.
