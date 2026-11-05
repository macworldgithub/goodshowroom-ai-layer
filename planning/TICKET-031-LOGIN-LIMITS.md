# TICKET-031: Login Integration Limits

Purpose: Give the user and coding agents an ordered list of work needed to
resolve the recorded login integration limits.

Status: Items 1, 2, 5 and 6 have isolated evidence. Item 4 includes the user's
real Google cancellation result. The user approved the item 5 operator procedure.
Item 3 remains deferred with site IDs open by explicit user choice. No real key
incident or deployment was performed. TICKET-031 remains In Review pending
acceptance of that deferred administrator setup.
The user authorized the listed work, a disposable local test database, and
test-only keys. Real provider access and any deployment still need their stated
target and prerequisites.

## Before starting an item

1. Read `AGENTS.md`, `Tickets.md`, `docs/DECISIONS.md`, the approved identity and
   security requirements, and the current code. `Tickets.md` remains the project
   task register. This file does not create new ticket IDs or change their status.
2. Work on the item the user selects. Continue through its authorized coding and
   verification. Record its actual results here and in the project records when
   required. Identify any task that belongs to another ticket before starting it.
3. Use synthetic data and isolated services by default. Never print or commit
   credentials, signed tokens, private keys, session cookies, or customer data.
   Do not change existing operational database credentials or source repositories.
4. For live Google tests, database writes, key provisioning, deployment, or
   destructive changes, obtain the authorization required by the project and
   confirm the exact target before acting. Prepare all safe work first.
5. Mark an item complete only when its completion checks pass. State what remains
   unverified. A simulated check is not evidence of a real provider or database.

## Current evidence

The Postman CLI cannot read its managed configuration. An existing MongoDB
service is running and has not been used. The user authorized a separate
disposable test database. A MongoDB 9.0 process listens only on 127.0.0.1:33719, with
replica set `ticket031test` and directory
`C:\Users\Mustafa Abrar\.codex\visualizations\2026\11\02\01a19a9a-9782-7860-b680-7176db29eab8\ticket031-mongodb`.
App-only indexes and transactions were checked with synthetic records; the test
records were removed and the indexes retained. It is unauthenticated and must
contain synthetic data only. Stop and remove this instance and its directory when
the project no longer needs it. Remind the user before removal.

Key lifecycle ownership also appears in TICKET-001. Any changes must preserve
the one-ticket workflow and the approved SEC-03 behavior.

TICKET-031 is In Review. Synthetic checks cover the mocked Google callback, real
frontend-to-backend HTTP transport with a database substitute, and real MongoDB
transactions through in-process backend routes. An authorized real Google login
and logout also succeeded on the isolated services. Other Google negative cases
remain open. No Postman simulation ran.

Relevant starting points:

- `README.md`, section **Google login integration**; `.env.example` and
  `frontend/.env.example` for server settings.
- `frontend/src/lib/server/auth.ts`, `frontend/src/lib/server/login-transport.ts`, and
  `frontend/src/app/api/auth/[...nextauth]/route.ts`.
- `src/goodshowroom_ai/login_setup.py`, `login_routes.py`,
  `login_lifecycle.py`, `identity.py`, `storage/mongo.py`, and `temporary.py`.
- `tests/conftest.py` and `frontend/scripts/test-network-policy.mjs` for the
  current test isolation. They intentionally block unapproved live access.

## 1. Local HTTP connection

**Status:** [x] Complete for isolated local HTTP; real Google remains separate.

**Problem:** Existing tests exercise the frontend and backend boundaries, but do
not prove the whole path over HTTP between running services. The Postman CLI
failed because it could not read a managed configuration file.

**Recommended solution:** Run the real code against isolated local stand-ins.
Use Postman simulation if it is available. Otherwise use a reviewed local test
service. Do not treat a stubbed `fetch` or a mocked response as proof of the HTTP
path.

**Directions:**

1. Check the current Postman configuration and any existing simulation. Do not
   bypass managed policy or retry a plan or usage restriction. If it is still
   unavailable, record the error and use an isolated fallback.
2. Define synthetic identities, keys, test-only configuration, and expected
   behavior from the approved contract. Check the target and port before starting
   a loopback-only service. Do not send test traffic to an existing service.
3. Exercise the actual frontend transport and backend route over HTTP. Include
   successful exchange, renewal, and revocation; invalid assertions; timeout or
   unavailable backend; and cleanup failure followed by retry. Check response
   status and resulting state, including that a failed logout is not reported as
   successful.
4. Keep test isolation for all other traffic. Do not globally disable the test
   network guard or allow real credentials into the test environment.

**Complete when:** The documented commands pass against the isolated services;
observed requests and state changes prove the relevant paths ran; the report
names the stand-ins, failure cases, and remaining real-service limits.

Evidence: `.venv/Scripts/python.exe -m tests.isolated_login_http` runs the actual
frontend transport against an actual loopback HTTP backend on port 8000. The
backend uses the real routes, verifier, workflow and lifecycle with an in-memory
database substitute and synthetic in-memory keys. It checks exchange and replay,
renewal, invalid assertion, injected 503, timeout, failed cleanup and retry, and
revoked state. It reserves the port before startup and blocks other test network
traffic. It does not exercise the real Google provider or MongoDB.

## 2. Real isolated MongoDB behavior

**Status:** [x] Complete for the designated isolated MongoDB instance.

**Problem:** The current transaction substitute does not prove real MongoDB
indexes, rollback, or concurrent updates. The application selects the fixed
`byd_ai_layer` database name, so the server or cluster itself must be isolated.

**Recommended solution:** Use a confirmed isolated replica set or test cluster
with disposable synthetic records. Check the approved index definitions and
exercise the real application repository and login routes.

**Directions:**

1. Ask the user to identify or authorize the exact isolated target and writes.
   Verify the host, database name, environment, and transaction support before
   connecting. Never use operational data or a shared production target.
2. Inspect the existing isolation controls and create a narrowly scoped, reviewed
   integration test path. `tests/conftest.py` currently blocks integration runs;
   do not remove that protection globally. Keep cleanup limited to the verified
   disposable target.
3. Explicitly prepare and verify the required app-only indexes on that target.
   Test new and repeated exchange, identity/email collision, concurrent exchange,
   renewal, revocation, failed audit rollback, and retry after a conflict.
4. Verify stored records and outcomes, then record the commands, target type,
   index evidence, and cleanup result without logging data or connection strings.

**Complete when:** The isolated database checks pass against real MongoDB and
show the required index, rollback, replay, and concurrency outcomes. State any
scenario that remains untested.

Evidence: `.venv/Scripts/python.exe -m tests.isolated_login_mongo` checks the
dedicated loopback replica set's process configuration and empty test database.
It creates and verifies app-only indexes, then runs signed routes with synthetic
records. It checks exchange and replay, email collision, concurrent exchange,
stale policy conflict and retry, renewal, failed audit rollback, and revocation.
It rechecks the target and ownership marker before deleting its own synthetic
records; it retains indexes. The real MongoDB driver and transactions are used.
This is one local MongoDB version and does not establish production behavior.

## 3. Test configuration and administrator setup

**Status:** [ ] Open

User approval on 2026-11-03: the first administrator may access all listed
branches and both holding yards, including DENZA Melbourne. Branch inclusion is
resolved. Canonical IDs, alias mappings and remaining grant details are not yet
approved. The generic `BYD Dealership` label remains unidentified and blocked.
No real grant was created. See the administrator approval record for the list.

The user selected the supplied Gmail account for the initial isolated-test
administrator and approved all business-data access for that administrator.
The authorization model still needs an explicit existing role and valid scopes.
No real business grant has been created. The Google client settings are present
in the repository `.env.local`. Both services load it over `.env`; process
settings retain priority. No values were printed. The actual administrator role
and business scope still need preparation; the isolated test used a synthetic role.

**Problem:** The login services are opt-in and need matching settings. The
initial administrator needs an approved email, an existing role, and an explicit
scope. The actual current values and test setup are not verified.

**Recommended solution:** Inventory setting names and presence without revealing
values. Prepare a separate test configuration and ask the user to supply or
approve the missing business choices and secrets through local configuration.

**Directions:**

1. Compare both `.env.example` files with the startup code and the isolated test
   target. Report only whether each required setting is present and valid. Do not
   print, copy to chat, or commit its value.
2. Agree the test administrator email, role ID, and scope with the user. Do not
   invent grants, create a production admin, or change existing credentials.
3. Prepare matching issuer, audience, key ID, and activation time on both
   services. Keep the private key in the Next.js server environment and the
   public key in the backend environment. Obtain authorization before generating
   or installing test keys. Preserve existing secrets and environment files.
4. Check missing, malformed, or mismatched settings fail closed. Document safe
   setup and verification steps, including the required existing test role and
   indexes. Do not enable the login service until its prerequisites are ready.

**Complete when:** The isolated configuration starts correctly with approved
settings; invalid settings fail safely; and no secret or grant is exposed in the
evidence. Record any user action still needed.

## 4. Real Google login

**Status:** [x] Complete for the approved local-account review, using the earlier
recorded real login/logout tests and the user's manual real cancellation test.
This is not production certification or independent observation of Google UI.

Latest manual evidence: the user reported that the updated Google flow showed
Cancel. The user selected it and returned to a local message asking them to try
signing in with a different account. The user confirmed this worked properly.
This closes the previously missing real cancellation observation. The generic
message does not require another Google account. No account session, credentials,
provider URL or token was inspected by the agent. No new backend-state check was
performed for this cancellation, so no database mutation outcome is claimed.
Older notes below that call cancellation unverified are superseded by this
explicitly user-observed result.

Consent-screen preparation: the isolated helper now sets
`GOOGLE_TEST_FORCE_CONSENT=true` and `APP_ENV=test`. The frontend adds only Google's
`prompt=consent` option. It preserves the provider's identity scopes and library
state/PKCE checks. Startup rejects the test option outside test mode or outside
the exact `http://localhost:3000` origin; malformed values also fail closed.
Normal login has no forced-consent option by default. No environment file changed.
Google documents this option as requesting consent again even after prior consent:
https://developers.google.com/identity/openid-connect/openid-connect

The frontend unit suite passed 75 cases, including four new consent-setting
cases. Type checking and lint passed. Ruff lint/format passed for the changed
Python helper. The old helper was stopped through its existing stop marker,
allowing its own test-record cleanup. The same isolated helper was restarted
with the approved account and the consent setting. MongoDB was not stopped.
Playwright MCP opened the local sign-in page and found the Google button. It did
not select it or inspect Google. This prepares a user-observed cancellation test;
it is not evidence that Google displayed the screen or returned cancellation.

For the next manual check, open `http://localhost:3000/api/auth/signin`, select
Google and use the approved account. On the Google consent screen, choose Cancel
if offered. Report only the local result. Do not send the provider URL, tokens,
screenshots or account information. The account-inspection restriction remains.

The user selected localhost and their Gmail test account. The Google Cloud web
client `byd-ai-layer` had no registered redirect URI. With user approval, the
callback `http://localhost:3000/api/auth/callback/google` was added and verified
after reopening the client. The consent application is External and in Testing
mode. The selected Gmail account was added as a test user and verified after
reloading the list. The selected account completed a real Google sign-in against
the local frontend and the isolated MongoDB replica set. The backend returned
200 and stored one approved test user and one active login. Sign-out returned
204 and the stored login was revoked. The test role had synthetic scope and did
not permit operational data. A separate run used the same Google account with a
different synthetic bootstrap email. The account remained pending and could not
load a business authorization policy. After sign-out, a fresh Google login made a
new binding while the earlier binding remained revoked. The user did not need a
second Google account. Real cancellation remains unverified. Do not put client
secrets in chat or test output.

A synthetic `access_denied` callback was sent to the isolated frontend and
returned a 302 to the local error page. This tests the local cancellation
handling only; it is not evidence that Google sent a real cancellation.

Follow-up user review: the isolated local services were started with the approved
account and the user reported reaching the GoodShowRoom page after Google sign-in.
The local backend reported successful login-route responses. The user reported no
Google cancellation button. This is a user-observed successful sign-in, not a
real cancellation result. A delegated Playwright MCP check opened the local home
page and confirmed it is a placeholder. The MCP could not create an isolated
browser context; its existing context opened the public local page. The test did
not inspect Google account sessions, authentication links, cookies or tokens.
Real cancellation remains open. The isolated services remain running for review.

2026-11-03 cancellation limit: Playwright MCP is available. The required browser
review was delegated to GPT-6.1 Terra with low reasoning. No Google session was
inspected. The prior automatic approval rejection remains in force. The browser
tools can return page state, full authentication URLs, headers or private account
data. No reviewed method was found to observe a real provider cancellation
within that restriction. No account inspection or workaround was attempted.
The real cancellation criterion remains open. Do not request another account.

User review can use the existing isolated helper with the approved account.
From the repository in PowerShell, set `LIVE_TEST_ADMIN_EMAIL` to the already
approved identity, then run `.venv/Scripts/python.exe -m tests.live_google_local`.
The helper needs the existing private local Google configuration and free ports
3000 and 8000. Open `http://localhost:3000/api/auth/signin`. Use the Google sign-in
button. If Google offers Cancel, select it. Expect a local authentication error
and no signed-in app session. Report only that outcome. If Google offers no
cancellation control, this check remains open. Do not send screenshots, account
details, cookies, tokens or authentication links. These services were not started
for this run. A user report would be manual evidence, not an independent provider
or database check. Stop the helper with Ctrl+C after review; keep MongoDB running.

The live test checks IPv4 and IPv6 wildcard port 3000 before it starts.
This detects a known conflict where another local website could receive the
Google callback. It also reports when a required test port is occupied. A small
race remains between releasing the port reservations and the frontend binding;
verify the active listeners before opening the browser.

The first real login failed with a 409 conflict. A synthetic test reproduced it
when its clock contained sub-millisecond precision: MongoDB stored timestamps at
millisecond precision and an in-transaction identity comparison failed. The
exchange now uses millisecond precision for stored timestamps. The synthetic
MongoDB test passed after the fix, followed by the successful real login.

**Problem:** The synthetic Google client does not prove a real Google callback
or Google RS256 ID-token verification. The existing live-test guard has no
reviewed Google adapter.

**Recommended solution:** After items 1–3, run an explicitly authorized test with
a designated Google test account and the isolated application database.

**Directions:**

1. Confirm the Google OAuth client, test account, exact local application URL,
   and callback URL with the user. Check the Google Cloud consent/test-user
   settings if applicable. The callback URL must match the registered value.
2. Prepare a reviewed live test path that enables only the specific Google and
   isolated database connections. Keep all other network and secret protections.
   Do not ask the user to paste credentials into chat.
3. With explicit authorization, test successful login, cancellation, unapproved
   account behavior, sign-out, and a fresh login after expiry or revocation.
   Verify the actual provider identity and backend binding using safe metadata.
4. Record whether the real Google ID token was verified, the target type, actions,
   and observed results. Do not claim business access, production readiness, or
   user-interface features that were not tested.

**Complete when:** The authorized real Google flow and negative cases pass with
the isolated database, and the report clearly separates them from synthetic tests.

## 5. Signing-key rotation and compromise response

**Status:** [x] Complete for the implemented single-process response and isolated
verification, with the operator procedure approved by the user. Real-environment
execution and the limits below remain explicitly unverified.

User review: the user explicitly approved the procedure to stop affected
services, reject the compromised key, invalidate all current logins, require a
new sign-in, and keep services stopped if saving or checking the change fails.
This approval does not authorize a real key change or deployment.
The technical review checked the procedure against the verifier, startup wiring,
database transaction and tests. The focused security/setup/login command
`.venv/Scripts/python.exe -m pytest tests/test_key_incident.py tests/test_login_setup.py tests/test_login_lifecycle.py -q`
passed 33 tests with synthetic data. No runtime code changed in this review.

2026-11-03 implementation: the operator function
`contain_signing_key_compromise` first locks the local verifier and closes the
single-process temporary store. Closing cancels active temporary work and drops
its context. It then writes permanent rejected key IDs, public-key fingerprints
and a global authentication-time cutoff into `app_settings/access_control`.
It uses a snapshot transaction with majority write confirmation and a five-second
commit bound. The shared policy fence, policy increment and required audit event
commit together. Required audit failure rolls back the database transaction.
Readback must confirm the committed state before the function reports success.

Login records have no reliable key provenance. The response therefore invalidates
all earlier logins, including logins made with an unaffected key. It rejects an
authentication time in or before the incident's UTC second. This also covers
legacy timestamps with sub-second precision. Old replay records remain stored.
A healthy replacement key needs a new verified Google sign-in after the cutoff.
Renewal, fresh login UUIDs and cookie refresh cannot revive the old sign-in.
No individual login rows need a bulk update.

Runtime assertion verification reads the committed registry before verification
and again after awaited login lookup. A renamed copy of a rejected public key
also fails. Registry reads have a five-second bound. Missing parent settings,
malformed state and database failure deny access. An old installation with no
incident field starts with an empty registry. Never delete that field or restore
an older backup over it after an incident. Restoring security state needs owner
review. A compromised database writer is outside this application guarantee.

Protected policy loads and login writes check the cutoff. Existing saved-turn
fences stop an old transaction snapshot from committing after the incident.
Final authorization checks stop late saved answers from release. Temporary work
is cancelled in the selected process. No claim is made that an already sent
provider request can be recalled or that host memory is physically erased.

There is no new HTTP endpoint. Signed assertions and app administrator roles
cannot grant the new internal `REVOKE_IDENTITY_KEYS` storage capability.
Only a trusted operator maintenance call uses it. No real key was revoked.

The existing `IDENTITY_PREVIOUS_KEYS` loader still supports planned overlap.
Configuration-only key removal is not a substitute for incident containment.

Approved operator procedure:

1. For planned rotation, prepare a different active signing key and its public
   verification key in approved server-only settings. Record the prior public
   key, its activation time, and its actual last-issuance time as `retired_at`.
2. Keep the previous public key available for at least 210 seconds after its
   last issuance. Never retain or copy its private key into backend settings.
3. For a suspected compromise, immediately stop the login and protected-work
   services and export. Stop private-key issuance. Obtain the owner's approval
   for the exact target and real security change. Do not use a grace period.
4. In trusted server maintenance code, load the affected public key metadata into
   the verifier. Use the fixed app database, the current policy version, the
   operator's opaque UUID, a new audit request UUID and the actual current UTC
   time. Call `contain_signing_key_compromise(db, verifier, memory, kid=...,
   actor_id=..., request_id=..., expected_policy_version=..., now=...)`.
   Pass the affected process's actual verifier and memory store when it is still
   running. An offline maintenance process must run only after serving processes
   have stopped. Never authorize this operation through the compromised signer.
5. On audit, write, cleanup, conflict, timeout or unknown-commit failure, keep all
   serving processes stopped. The local verifier stays locked. A failed write
   does not prove durable revocation. Reconnect and read the current registry and
   policy version. Retry the operation while stopped. Do not resume from a
   process restart or an old backup before a confirmed durable result. An
   unknown result can have committed; the repeat preserves prior rejected keys
   and never moves the cutoff backwards. It can invalidate newer logins too.
6. Check the required audit event and registry on the approved target. Provision
   a different key only with the owner's approval. Keep the registry and replay
   records. Rebuild the verifier and memory store; they have no unlock operation.
   Verify rejected-key, old-login and new-sign-in behavior before resuming.
   Require every user to sign in again after the cutoff. Real provisioning,
   service restart and deployment are not authorized by this synthetic test.

Evidence: `tests/test_key_incident.py` has 20 offline synthetic cases. They cover
fresh verifier/database objects, stale verifier state, renamed public keys,
global invalidation, old authentication replay, fresh sign-in, cutoff precision,
temporary work cancellation, late saved-answer denial, stale commit versions,
concurrent incidents, required audit rollback and retry, malformed state,
unavailable reads, readback failure, internal capability denial and majority
transaction settings. They also deny a key removed during the last registry
read. These use a database substitute and a simulated model.

The extended `.venv/Scripts/python.exe -m tests.isolated_login_mongo` passed on
the approved replica set at 127.0.0.1:33719. It checks real incident audit rollback,
durable rejection after fresh database/verifier construction, old login denial,
healthy-key fresh authentication, two competing incident transactions and an
in-flight old snapshot that cannot cross the commit fence. It also checks the
existing login routes, indexes and replay cases. It removes its synthetic records
and retains indexes. This is one local MongoDB version. It reconstructs backend
objects; it does not restart the MongoDB process or prove production failover.

The first attempt found the approved test instance stopped and made no writes.
The same saved test instance was restarted after checking its path and port.
A later run repaired a test's expected denial type: a repository transaction
converts the verifier's permission error to a safe storage authorization error.
The final isolated run passed. The separate MongoDB service was not used.

Final commands on 2026-11-03, from the repository unless stated otherwise:

- `.venv/Scripts/python.exe -m pytest -q`: 963 passed. This includes the 20 new
  incident cases. Test network and private-environment guards remained active.
- `.venv/Scripts/python.exe -m ruff check --no-cache .`: passed.
- `.venv/Scripts/python.exe -m ruff format --no-cache --check .`: 92 files passed.
- `.venv/Scripts/python.exe -m tests.isolated_login_mongo`: passed on the approved
  target. Synthetic records were removed and indexes retained.
- `.venv/Scripts/python.exe -m tests.isolated_login_http`: passed. Actual frontend
  transport and backend HTTP routes used a synthetic database substitute.
- In `frontend`, `npm run test:unit`: 71 passed. `npm run typecheck`,
  `npm run lint`, `npm run contracts:check` and `npm run check:setup`: passed.
  Setup includes a build, browser-asset scans and temporary local HTTP checks.
- `git diff --check`: passed.

The old setup check only expected `.env` values. The approved `.env.local`
priority made that check fail. It now checks process/local/shared priority,
private values from both files and unchanged file hashes. No environment value
was printed or changed. The generated frontend type-reference file was restored
to its starting content after the build. Initial sandbox permission errors were
resolved by running the approved checks outside the sandbox. No safety guard
or managed policy was removed. Postman cannot read its managed Private Cloud
manifest; no Postman simulation ran.

**Remaining limits:** No real key incident, deployment, host compromise, database
rollback recovery or multiple-worker cleanup was tested. Procedure approval and
isolated evidence do not establish those results. Items 3 and 4 keep the overall
ticket In Review.

**Recommended solution:** Design and test a server-only key registry and an
operational procedure against the approved Security rules before using real keys.

**Directions:**

1. Read SEC-03 in `docs/09-SECURITY.md` and the existing signer/verifier. Check
   ticket ownership and current key/session storage before choosing changes.
2. Implement or wire approved key ID selection, issuance age, previous-key overlap,
   and immediate compromised-key rejection. Address affected login generations
   and in-flight work; do not claim they are invalidated without proving it.
3. Use synthetic keys to test normal rotation, overlap expiry, unknown key IDs,
   compromise with no grace period, restart behavior, and affected-session
   invalidation. Keep private keys out of output and source control.
4. Prepare a reviewable operator procedure. Obtain authorization before any real
   key generation, rotation, or session invalidation.

**Complete when:** The approved lifecycle is implemented and verified with
synthetic tests, the operator procedure is reviewed, and any real-environment
verification remains explicitly recorded until it is performed.

## 6. Temporary-session cleanup and process count

**Status:** [x] Complete for the selected single-process model.

**Problem:** Temporary-chat cleanup is implemented for one backend process.
Cleanup across multiple processes has not been designed or verified.

**Recommended solution:** Keep and verify the approved single-process design
unless the user explicitly selects a different deployment model.

**Directions:**

1. Confirm the intended process count with the user and compare it with the
   approved Architecture and Security documents. Do not assume multiple workers
   are required.
2. For one process, test logout, cancellation, expired leases, and the cleanup
   sweep against the real temporary-memory service. Record the process boundary
   and any failure limits.
3. If the user wants multiple processes, first propose the coordination design,
   required ticket/architecture changes, and failure bounds for review. Implement
   it only after the necessary authorization. Test notification failure and
   expiry across processes without persisting temporary chat content.

**Complete when:** The selected process model and its cleanup behavior have
appropriate evidence. For a single-process release, explicitly retain the
multi-process limitation instead of claiming it has been solved.

Evidence: the user selected one process. Synthetic tests exercise the real
temporary-memory store's logout, work cancellation, lease expiry and sweep,
including signed logout through the backend route. The store is process-local;
multi-process cleanup is not implemented or verified. A failed notification can
leave memory until the lease and sweep limits described in SEC-11. This does not
promise physical erasure of RAM.

## Recommended order

Start with item 1. Then do items 2 and 3 before the authorized live Google test
in item 4. Work on item 5 before relying on an operational key lifecycle. Resolve
the process-count decision in item 6 before a deployment that needs it.
