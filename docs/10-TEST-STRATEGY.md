# Good Showroom AI Chatbot - Test Strategy

Purpose: Define reproducible checks for correct answers, access boundaries, privacy, and ticket completion.

Status: Approved by User  
Updated: 2026-10-22  
Approval: The user approved Test Strategy on 2026-10-22 and authorized the Implementation Plan draft only. No coding or test execution is authorized.
Basis: Approved [PRD](01-PRD.md), [Scope](02-SCOPE.md), [User Flows](03-USER-FLOWS.md), [Architecture](04-ARCHITECTURE.md), [Tech Stack](05-TECH-STACK.md), [Database Schema](06-DATABASE-SCHEMA.md), [API Contracts](07-API-CONTRACTS.md), [UI/UX](08-UI-UX-GUIDELINES.md), and [Security](09-SECURITY.md). Gates, command conventions, and evaluation targets below are approved designs, not implemented scripts or recorded results. No tests or application code were created by this documentation work.

## 1. Test Levels and Tools

| Level | What it proves | Approved tools |
|---|---|---|
| Unit | One function/service handles valid, invalid, and denied inputs | Pytest/pytest-asyncio; injected model, clock, policy, repository, and exporter |
| API/contract | Real routes enforce identity, validation, safe errors, and response shapes | HTTPX with FastAPI; matching frontend type/runtime checks |
| Database integration | Actual indexes, queries, transactions, conflicts, and cleanup work | PyMongo against isolated synthetic test databases |
| UI/component | Forms, Markdown, tables, charts, and states render correctly | Vitest and React Testing Library |
| End-to-end/manual | Both services support the complete user journey | Playwright and user review; synthetic data by default |

Mocked tests prove controlled behavior, not actual model accuracy or live integration. FastAPI tests explicitly exercise startup/shutdown with injected dependencies and reset overrides; an async HTTP client alone does not run lifespan. See [FastAPI async tests](https://fastapi.tiangolo.com/advanced/async-tests/) and [Pytest isolation](https://docs.pytest.org/en/stable/how-to/monkeypatch.html).

## 2. Isolation and Repeatability

- Default runs load test configuration, not the real `.env`; block external network/model/Google/trace calls. Generate throwaway signing keys in memory. Commit no credentials, customer records, or mock passwords.
- Freeze time/timezone and use deterministic fixtures/seeds. Reset app state, dependency overrides, environment changes, and temporary memory after each case. Use controlled race barriers/fake clocks instead of long sleeps.
- DB integrations require a verified test-only target/namespace. Reject real operational names and `byd_ai_layer`, unsafe host/namespace combinations, and unverified cleanup targets. Map logical source names to synthetic databases explicitly. Cleanup only this run's verified fixture namespace; a `_test` suffix alone is not proof of safety.
- Model/DB/exporter mocks support failures and cancellation. Security suites exercise real validators with runtime test keys, not an allow-all auth mock. A DB mock cannot prove MongoDB transaction/index behavior; report real integration as pending if unavailable. Do not provision a server, change source credentials, run sibling services, or touch operational records for this draft.
- Disable content-bearing browser traces, screenshots, videos, snapshots, and HTTP-body recording for temporary/privacy cases. Inspect sentinel captures in memory; persist only safe assertion outcomes. No external evaluation/monitoring platform or new application service is needed.

## 3. Required Coverage

Tests map to PRD/ARCH requirements, SEC-01 through SEC-15, and ticket acceptance criteria. Each protected operation needs allowed, denied, invalid-input, and dependency-failure cases.

| Area | Required cases |
|---|---|
| Identity/admin (SEC-01–04) | Google verification/provider binding; pending/preapproved/rejected/revoked users; email collisions; bootstrap/last-admin races; forged/wrong-purpose/expired/operation-mismatched assertions; algorithm/key rejection, rotation/revocation, replay, CSRF/Origin/CORS. |
| Data access/disclosure (SEC-04–06) | Role narrowing; cross-user/site/record/field/collection/join attacks; missing staff mappings; authorized records before counts; invalid cursors; stale policy at tools/commit/release; small-group/complement/differencing attempts. Undefined disclosure policies must deny the affected breakdown. |
| Read repository (SEC-06) | No mutation/raw-driver interface; spies capture only approved reads. Reject write commands/stages, nested escapes, server JavaScript, operator/regex injection, and unapproved joins before driver execution. Never probe production with a write. |
| Metrics/evidence (SEC-07) | Q-01–Q-05; exact counts; source/status/site normalization; original-date provenance; missing/duplicate/unmatched records; calendar/rolling periods, month boundaries, DST and leap days; zero versus partial/missing/failure; unsupported conversions. Same verified facts in text/cards/charts/tables. |
| AI/context (SEC-07) | Approved tool/argument selection; ambiguous questions; follow-up filters; no cross-chat memory; bounded complete tool pairs/summaries; refresh stale facts. User/stored-text injections cannot change grants/tools or add numerical claims. Invalid output is rejected or rebuilt deterministically. |
| Contracts/rendering/limits (SEC-08–09,14) | HTTP codes/envelopes, headers/cookies, schema drift, safe Markdown/links/chart bindings, no secret bundles, query/pagination/rate/token/byte bounds, malformed IDs, oversized/deep input, cancellation and source/model timeouts. A limited page never becomes a total. |
| Saved turns (SEC-10,13) | Atomic pair/state/audit; uniqueness; concurrent turns; same request replay/changed payload; stale version; failures before/during/after commit; unknown commit reconciliation; lost response/restart recovery; deletion retry and blocked commits. Owner/admin review requires current grants and audit; audit failure denies required action. |
| Temporary lifecycle (SEC-11) | User/login binding, mode isolation, end/logout/auth expiry/idle/lease/restart, failed notifications, cleanup sweeps, late replies. Verify no temporary content/IDs/hashes in MongoDB, persistent browser storage, logs, SDK persistence, analytics, or artifacts; no history/admin-review route. |
| Tracing/audit (SEC-12–14) | Environment/flag matrix, sensitive capture off, replaced default processors, no parallel exporter, full serialized export/log sentinel checks, unknown-field rejection, startup/flush/shutdown errors, metadata-only temporary diagnostics. Failure stays nonfatal; real authorization failures still deny. Test no app trace endpoint; verify developer project access separately. |
| UI/operations (SEC-08–09,15) | Login/access/chat/admin/review journeys; notices; truthful save/unknown states; keyboard/focus, zoom, reduced motion, mobile tables/charts; permission-loss clearing. Review locked dependencies/copied UI code, sanitized errors, and compromise/revocation procedures. |

Boundary tests use approved settings: 120-second assertions/30-second tolerance, eight-hour absolute login, five-minute lease, 30-minute idle, 60-second sweep, 90-day key age/210-second rotation overlap, and API/Security size/execution limits. Test just below, at, and above each bound; tolerance never extends a login lease. Freeze clocks rather than waiting hours.

## 4. Business Question and Agent Evaluations

Maintain a versioned synthetic corpus with at least four phrasings per enabled Q-01–Q-05 family, plus clarification, denied, unsupported, follow-up, and adversarial cases. Each case records requirement/ticket, fixture version, frozen clock/timezone, grants/mappings, conversation prefix, allowed tool sequence/arguments, expected facts/status/coverage, and visual dataset. Expected results are reviewed independently of the code under test; do not calculate them using that same implementation.

**Example fixture:** freeze dealership date at 2026-10-22 in `Australia/Melbourne`. "Last seven days" is `[2026-10-15, 2026-10-22)` locally. Include two permitted reliable-date leads inside the period, one exactly at its excluded end, one from a forbidden site, and one permitted record with unknown received date. Expect **two verified leads with partial/unknown-date coverage**, not an unsupported exact overall total. Text/card agree; no forbidden-site count is disclosed. Fixtures are invented test data, not the earlier live inventory.

Scripted model responses verify orchestration/validation. Optional live evaluations use only synthetic data, the configured model, explicit spend/network opt-in, and recorded prompt/model/metric versions. Run each question variant three times. Approved targets: at least **95%** correct intent/tool/arguments overall; **100%** numerical/evidence consistency and security/privacy cases; no unauthorized tool execution. Score facts/actions, not exact prose or an LLM judge alone. Missing required facts/status fails; blanket denials cannot pass allowed-question cases. A live regression blocks claiming that model setup ready, not offline development. Mocked scores never stand in for live scores.

## 5. Browser and Real-Integration Checks

Use role/label-based selectors, independent browser contexts, and observable assertions rather than fixed delays, following [Playwright guidance](https://playwright.dev/docs/best-practices). Start with Chromium desktop/mobile viewports; the final supported-browser matrix remains open. Manual checks cover focus order, readable charts, notices, and complete user journeys; automation alone does not certify accessibility.

Required journey: approval/preapproval, saved last-seven-days question/card, follow-up, reopen, authorized admin review, Temporary Chat/end/non-persistence, and logout. Label mocked versus real login explicitly.

Follow [AGENTS.md](../AGENTS.md): the user normally performs browser review. When agent-driven browser verification is explicitly requested, use Playwright MCP and its required delegation rule; if unavailable, report that and use an appropriate available fallback. No browser check or delegation is requested by this document draft.

Mock Google for routine tests. A separately authorized real Google sign-in verifies callback/cookies/identity binding using designated accounts; never automate or record passwords. Real DB adapter/query-plan checks and actual native trace compatibility need separate safe evidence. Do not infer provider retention/MFA/dashboard restrictions from mocks. Real customer export, operational mutation, and credential changes are outside ordinary tests.

## 6. Planned Commands and Completion Gates

During authorized implementation, expose these conventions from the owning Python/frontend package directories. Paths/manifests/scripts are not created here. Explicit markers separate default offline tests from opt-in integrations/live evaluations; missing marker/configuration must fail safely, not enable network access.

| Check | Planned command |
|---|---|
| Backend style | `uv run ruff check .` and `uv run ruff format --check .` |
| Offline backend | `uv run pytest --strict-markers -m "not integration and not live"` |
| Test-DB integration | `uv run pytest --strict-markers -m "integration and not live"` after verified isolation setup |
| Optional live evaluation | `uv run pytest --strict-markers -m live` after explicit opt-in |
| Frontend static/component | `npm run lint`, `npm run typecheck`, `npm run test:unit` (single-run Vitest) |
| Local end-to-end | `npm run test:e2e` (installed Playwright runner, synthetic services/data) |

Commands follow [Pytest selection](https://docs.pytest.org/en/stable/how-to/usage.html), [Ruff checks](https://docs.astral.sh/ruff/formatter/), [Vitest single-run mode](https://vitest.dev/guide/cli), and [Playwright CLI](https://playwright.dev/docs/test-cli). These are approved future interfaces, not runnable project scripts or recorded results.

For the selected ticket, require all applicable acceptance checks and static checks to pass; zero unauthorized disclosures/writes, unsupported numerical claims, partial/duplicate commits, or temporary persistent content. Map every applicable SEC rule to positive/negative assertions. Report coverage gaps; a line-coverage percentage is not security proof. No hidden skips, `xfail`, flaky reruns, or snapshot updates to hide a failure. Unavailable verification stays pending; no Done claim until criteria and required review are met.

Record ticket ID, revision, tool/fixture/model versions, exact command/working directory, pass/fail/skip counts, safe output, manual results, limitations, and review reference in the ticket's Completion Evidence. Update Progress consistently; no separate task register. TICKET-014/015/016 hold the existing evaluation work; feature tickets include their relevant tests.

## 7. Open Values and Review

Retention/disclosure policies and source-date/staff mappings still need approved values. During implementation, test safe denial/default behavior and success using clearly labeled synthetic policies. Never treat fixture choices as approved business rules. Browser support, hardware-based performance baselines, model-evaluation budget, and provider/MFA evidence remain to confirm. Measure latency/usage safely; approved deadlines are limits, not measured speed promises.

Test Strategy and [Implementation Plan](../planning/IMPLEMENTATION-PLAN.md) are approved; [Tickets.md](../Tickets.md) refinement is the current review item. Do not execute tickets or tests before explicit implementation authorization. No tools/tests are installed/created and no application/browser/database/live-model tests ran in this documentation step.
