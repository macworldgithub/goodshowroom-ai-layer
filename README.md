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
