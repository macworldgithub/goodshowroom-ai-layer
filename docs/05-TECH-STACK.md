# Good Showroom AI Chatbot - Technology Stack

Purpose: Recommend a small, compatible set of tools for the approved architecture.

Status: Approved by User  
Updated: 2026-10-22  
Approval: The user approved this Tech Stack on 2026-10-22 and authorized the Database Schema draft only. No coding or package installation is authorized.
Basis: Approved [Architecture](04-ARCHITECTURE.md), [PRD](01-PRD.md), [Scope](02-SCOPE.md), and [User Flows](03-USER-FLOWS.md). Choices below are approved recommendations, not an installed or tested environment; candidate pins still require joint resolution/verification during implementation.

UI approval note (2026-10-22): The user approved [UI/UX Guidelines](08-UI-UX-GUIDELINES.md) and shadcn/ui components, TanStack Table where advanced table behavior is needed, and react-markdown with optional remark-gfm. Recharts, React state, and `fetch` are retained. Package compatibility/pins remain for authorized implementation; no installs or backend-stack changes are made here.

## 1. Runtimes and Frontend

| Area | Proposed choice | Reason |
|---|---|---|
| Frontend runtime | Node.js 24 LTS; npm | A supported long-term release and one familiar package manager. Use the latest security patch within this branch. |
| Screens/server routes | Next.js 16.4.2 App Router; React/React DOM 19.3.1 | One application for screens, login, and the server-only identity bridge. |
| Types | TypeScript 5.9.3, strict mode | A conservative stable baseline within the checked lint-tool compatibility range; do not automatically select TypeScript 7. |
| Styling | Tailwind CSS 4.3.3 with matching PostCSS integration | Consistent responsive styling without a large component framework. |
| Google login | Stable `next-auth` 4.24.15 | Preserves the selected library. Its declared peers include Next.js 16 and React 19. V4 is in maintenance mode; V5 remains beta, so do not silently mix their APIs. |
| Visuals | Recharts 3.10.1; matching `react-is` 19.3.1 | Bar/line charts from the backend's validated dataset. Use ordinary React cards and semantic HTML tables with accessible alternatives. |

Use React state and built-in `fetch` initially. Keep protected browser calls on Next.js server routes; only FastAPI accesses MongoDB. Check the session on the server using the selected V4 APIs. Use NextAuth's JWT-cookie strategy without a Node/Mongo adapter; FastAPI remains the authority for app users, approval, roles, revocation, and login-session records. The NextAuth cookie and internal signed identity token are separate mechanisms.

## 2. Backend and Data

| Area | Proposed choice | Reason |
|---|---|---|
| Backend runtime | Python 3.13, latest security patch | A supported common baseline compatible with the checked package requirements. |
| HTTP service | FastAPI 0.159.1; Uvicorn 0.54.0 | Typed async endpoints in one backend process. A restart/reload discards temporary context. |
| Validation/configuration | Pydantic 2.14.1; pydantic-settings 2.16.1 | Validate tools, evidence, responses, and environment settings explicitly. |
| AI orchestration | `openai-agents` 0.24.0; compatible `openai` 3.36.1 | One primary agent with typed tools, bounded explicit input, and native developer tracing. Keep the existing configurable `OPENAI_MODEL`; no model change is selected here. |
| MongoDB access | PyMongo 4.19.0, `AsyncMongoClient` | Official async access with connection pooling. Choose PyMongo Async for this new backend; leave the reference repos' Motor code unchanged. |
| Identity token libraries | Node `jose` 6.x; Python `PyJWT[crypto]` 2.x | Established signing/verification tools for the server-only bridge. Exact algorithm, keys, rotation, and lease values belong in Security. |
| Time handling | Standard-library `datetime`/`zoneinfo`, plus `tzdata` | Dealership calendar days and daylight-saving handling, including Windows systems without an IANA timezone database. |

Use plain Python services/repositories for AuthorizationService, MetricRegistry, ResponseBuilder, and ChatRepository. No new authorization framework, ORM/ODM, or agent-memory library is needed. Pydantic/FastAPI OpenAPI schemas define backend contracts; keep frontend types and runtime rendering checks consistent with those contracts.

## 3. Storage, Configuration, and Debugging

- Keep current MongoDB credentials and permissions. OperationalReadRepository enforces reads in application code; app writes target only `byd_ai_layer`. No MongoDB server upgrade or source-schema/index change is selected.
- ChatRepository owns atomic validated-turn commits. Prepare agent context explicitly; do not attach permanent storage as an automatically saving SDK session or use provider-managed continuation. Temporary context remains process-memory-only.
- Load the existing ignored root `.env` explicitly in each server's startup configuration. Do not assume a future nested frontend directory automatically loads it. Preserve user values; never expose credentials through `NEXT_PUBLIC_*` variables or client code.
- Map existing Google/auth settings explicitly to the chosen NextAuth configuration. Map `APP_ENV` and tracing flags to SDK controls; sensitive capture stays off for real/temporary data. The optional existing `OPENAI_TRACING_KEY` is an app setting that must be explicitly supplied to the native exporter; it is not an SDK auto-detected variable. Use the permitted model-project key when appropriate if no separate tracing key is supplied; export failures remain nonfatal.
- Keep supported foreground model requests at `store=false`. Sanitized native developer tracing is separate from model-response storage and app chat history. The approved export/privacy rules stay in Architecture section 9; add no monitoring platform.

## 4. Development and Verification Tools

Recommend **uv** with a project-local `.venv`, `pyproject.toml`, and `uv.lock` for Python; **npm** with `package-lock.json` for the frontend. Lockfiles record exact direct and indirect versions so later agents can reproduce the environment. These files/tools are not created or installed in this step.

| Purpose | Proposed tools |
|---|---|
| Python lint/format | Ruff |
| Backend tests/evaluations | Pytest, pytest-asyncio, HTTPX; injected model/policy/clock/exporter dependencies and isolated Mongo test data |
| Frontend checks | TypeScript compiler, ESLint with matching Next.js config, Vitest and React Testing Library |
| End-to-end verification | Playwright; use Playwright MCP for agent-driven browser verification when requested, following AGENTS.md |

Define reproducible commands, thresholds, and detailed coverage in the later Test Strategy. This draft adds no tests and claims no test results.

## 5. Version Policy and Review

Implementation note (TICKET-018, 2026-10-23): the frontend setup pins ESLint
9.39.5 because Next.js 16.4.2's import/React/accessibility plugins reject ESLint
10 in their declared peers. The current TypeScript lint plugin accepts 10, but
that alone does not prove the whole stack compatible. Strict npm peer checks
remain enabled. ESLint 9 reached end-of-life on 2026-08-06; this is an explicit
review limitation, not a claim of maintained tooling. Revisit when compatible
upstream plugin releases exist. See [ESLint support policy](https://eslint.org/version-support/).

The setup audit also reports five high entries through the development-only
braces chain, with no patched release listed for
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm).
The production-only audit has zero findings. These are separate from ESLint's
support status. No forced override or framework downgrade was applied; tooling
limitations remain explicit for TICKET-018 review.

Release/peer metadata was checked on 2026-10-22. Listed patch versions are **candidate pins**, not a proven joint installation. During authorized implementation, resolve/lock the complete environment and verify imports, types, auth, database adapters, and trace-export compatibility before claiming support. In particular, Agents SDK 0.24.0 declares `openai>=3,<4`, `pydantic>=2.12.2,<3`, and `starlette>=1.3.1`; resolve FastAPI and these transitive dependencies together. Do not copy the reference backend's older dependency file or bypass peer conflicts.

Review upgrades deliberately; keep related React packages aligned and add libraries only for a demonstrated need. There is no Redis, queue, vector database, additional agent, deployment/CI setup, or extra service. Atomic schema layout, token/security settings, retention, metric semantics, and performance/context limits remain for their approved document steps.

Sources: [Node releases](https://nodejs.org/en/about/previous-releases), [Next.js installation](https://nextjs.org/docs/app/getting-started/installation), [NextAuth release/peers](https://registry.npmjs.org/next-auth/4.24.15), [Auth.js release status](https://authjs.dev/getting-started/integrations), [TypeScript tooling peers](https://registry.npmjs.org/typescript-eslint/8.72.0), [PyMongo Async](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/), [Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk), [SDK release requirements](https://pypi.org/pypi/openai-agents/0.24.0/json), [Recharts](https://recharts.github.io/en-US/guide/installation/), [timezone data](https://docs.python.org/3/library/zoneinfo.html), and [uv lockfiles](https://docs.astral.sh/uv/concepts/projects/sync/). Publisher npm/PyPI metadata supplied the candidate versions; checks did not install packages or contact business databases.

Tech Stack is approved. `06-DATABASE-SCHEMA.md` is the current draft for review. Do not execute Backlog tickets or start coding until authorized.
