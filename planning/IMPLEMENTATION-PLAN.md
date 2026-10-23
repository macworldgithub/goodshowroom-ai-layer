# Good Showroom AI Chatbot - Implementation Plan

Purpose: Sequence the approved work, identify ticket coverage gaps, and define review checkpoints.

Status: Approved by User  
Updated: 2026-10-22  
Approval: The user approved this plan on 2026-10-22 and authorized Tickets.md refinement only. No implementation is authorized.
Basis: Approved documents 01–10, especially [Scope](../docs/02-SCOPE.md), [Architecture](../docs/04-ARCHITECTURE.md), [Schema](../docs/06-DATABASE-SCHEMA.md), [API](../docs/07-API-CONTRACTS.md), [UI/UX](../docs/08-UI-UX-GUIDELINES.md), [Security](../docs/09-SECURITY.md), [Test Strategy](../docs/10-TEST-STRATEGY.md), and [Tickets.md](../Tickets.md). Owner/prerequisite references below are synchronized with the requested refinement, which awaits ticket review. Approved milestones, architecture, and review gates are preserved.

## 1. Working Rules

- Tickets.md owns task IDs, dependencies, acceptance criteria, and status. This document orders work; it is not another task register. The original 16 IDs are preserved and 42 tickets added; all 58 remain Backlog pending review/authorization.
- After explicit implementation authorization, select one approved Ready ticket with satisfied dependencies. Read its requirements, plan a small change, implement/test it, present evidence, and obtain required review. Continue only when authorized.
- Add necessary setup/tests within a ticket's stated scope. If setup or feature work has no ticket, register it first during authorized refinement; do not hide it inside an unrelated ticket. Split oversized tickets before execution.
- Work only in `goodshowroom-ai-layer`. Keep two services, one backend process, one agent, approved libraries, current credentials, and operational schemas/indexes. No deployment, CI, extra services, or feature expansion.

## 2. Ticket Readiness and Coverage

The original 16 tickets covered architecture foundations. The requested refinement now records explicit owners for the missing work below; their proposed decomposition/dependencies await review in Tickets.md:

| Approved requirement | Refined ticket coverage |
|---|---|
| Runnable local workspace, manifests/locks, configuration loading, and isolated test harness | TICKET-017–020; approved [Tech Stack](../docs/05-TECH-STACK.md) and Test Strategy. |
| App users/roles/access requests/preapprovals/mappings/settings/audits and indexes | TICKET-021–024/027/028/029 plus atomic turns in TICKET-008; bootstrap/access workflows in TICKET-025/026. |
| Complete Next.js/FastAPI routes and access-management workflows | TICKET-030–039, web boundary TICKET-056, admission/cursors TICKET-057; use existing services/policy. |
| Login/status, admin settings, chat/history/visuals, and conversation-review screens | TICKET-040–049; first slice TICKET-054, full local acceptance TICKET-055 and runbook TICKET-058. |
| Small first metric followed by remaining approved question families | TICKET-005 owns Q-01; TICKET-050–053 add Q-02–Q-05 after the first slice. |

This is a coverage map, not a second task list or implementation authorization. The refined graph puts storage/login prerequisites before TICKET-001; bootstrap/decisions and grant management follow AuthorizationService in TICKET-025/026. Thus TICKET-001 no longer implicitly requires TICKET-002. Review the register before selecting one implementation ticket.

Do not add new credential/Atlas prerequisites. Use synthetic identities/data and injected services for early work. Check real configuration only when its authorized ticket needs it; preserve values and never print secrets.

## 3. Foundation Sequence and Refined Prerequisites

This table summarizes the original foundations, not a complete 58-ticket execution order. Insert every new prerequisite from the authoritative Dependencies fields before its dependent; later-numbered IDs are not later delivery dates. Groups do not authorize parallel work.

| Stage | Existing ticket order | Evidence before advancing |
|---|---|---|
| A. Identity and permissions | TICKET-001 → TICKET-002 | Signed bridge and current policy denial after registered storage prerequisites. Bootstrap/last-admin/access workflows follow in TICKET-025/026; Google integration is TICKET-031. |
| B. Controlled source reads | TICKET-003 → TICKET-004 | No mutation/driver escape; bounded authorized queries/joins; deterministic date/ID/site/staff normalization and coverage warnings. |
| C. Verified answers | TICKET-005 → TICKET-006 → TICKET-007 | MetricRegistry definitions, safe aggregate policy/default denial, exact counts, matching text/visual data, and validated complete responses. |
| D. Saved conversation state | TICKET-008 | TICKET-028 provides parent sessions first; TICKET-008 commits atomically; TICKET-029 owns filtered history/deletion. Real Mongo evidence is required for transaction/index claims. |
| E. Safe tracing setup | TICKET-011 → TICKET-012 | Explicit disablement/configuration and sanitized exporter replacement verified before any agent trace can leave the process. Use captured synthetic exports; keep real export off until its checks pass. |
| F. Agent and temporary context | TICKET-009 → TICKET-010 → TICKET-013 | Bounded authorized context/tools, no automatic permanent SDK writes/provider continuation, memory-only temporary lifecycle/logout, and safe stage diagnostics. |
| G. Cross-boundary verification | TICKET-014 → TICKET-015 → TICKET-016 | Reproducible metric/agent corpus; access/injection/mutation defenses; commit/temporary/trace failure tests. Publish safe results and unresolved verification. |

Tests/static checks start with each ticket. Stage G adds broader coverage, not the first security tests. The refinement makes TICKET-011/012 explicit dependencies of TICKET-009 to prevent an unsafe export window. Tracing stays disabled when unready. Setup candidates are TICKET-017/018; neither is Ready or authorized yet.

## 4. Delivery Checkpoints

**First end-to-end milestone (TICKET-054):** an approved Google user asks for leads received in the previous seven completed dealership days, receives a correct card with source/period/timezone/coverage, asks a follow-up, and reopens the saved chat. Follow its registered API/UI/core prerequisites. TICKET-005 is narrowed to Q-01; uncertain source/rules get coverage/unavailable states, not invented totals. Synthetic Google/model evidence is labeled explicitly.

**Complete version 1 (TICKET-055):** TICKET-050–053 follow the first slice; remaining registered UI/admin/temporary work and TICKET-058 complete enabled-scope acceptance. Undefined pending/status/staff/date/disclosure meanings keep only the affected capability unavailable. Delivery/conversion/inventory/contractor extensions remain outside this release.

**Integration readiness:** distinguish fixture demonstrations from real Google, MongoDB, configured-model, and native-trace evidence. Optional live checks use synthetic data, explicit authorization/budget, and the approved test controls. Do not claim integration/model readiness from mocks. These are review checkpoints, not additional implementation tasks outside Tickets.md.

## 5. Decisions Resolved When Needed

| Decision/evidence | Latest point it is needed | Safe behavior meanwhile |
|---|---|---|
| Package compatibility, runtime imports, manifests/lockfiles/test commands | Registered local setup | Approved candidate versions remain unverified; no forced peer bypass. |
| Original received dates, exclusions/deduplication, site timezone, staff mappings | Relevant TICKET-004/005 or question enablement | Clearly labeled fixtures; unsupported/partial responses and no widened access. |
| Small-group/complement/differencing policy | Sensitive breakdown enablement in TICKET-006 | Withhold affected breakdowns; implement/test safe denial without inventing thresholds. |
| App transaction capability, retention/deletion settings | Relevant app-storage work/TICKET-008 | Isolated tests; no silent non-atomic fallback or unreviewed TTL. Retention values need approval before claiming retention behavior. |
| Trusted first-admin email/callback and runtime keys | Authorized login integration | Synthetic identities/ephemeral keys; no operational credential rotation. |
| Provider access/retention, admin MFA assurance, browser targets, live-evaluation budget | Relevant real-data/export/readiness claim | No unsupported assurance; tracing off when its requirements are unverified. |

Do not guess missing business rules. Record approved decisions in [DECISIONS.md](../docs/DECISIONS.md). A local unresolved value limits its affected feature/check, not all offline development.

## 6. Completion and Review Gates

Use the approved Test Strategy: applicable acceptance/static/security checks pass, numerical/evidence consistency is exact, and no unauthorized source write/disclosure, duplicate/partial turn, or persistent temporary content occurs in tested cases. Real-DB and optional live-model evidence remain separately labeled. Preserve the approved evaluation targets; do not change thresholds to make a run pass.

For each ticket, record actual commands, working directory/revision, fixture/tool/model versions, results/skips, safe evidence, limitations, and review in Completion Evidence. Update [PROGRESS.md](PROGRESS.md) from the register only. Failed/unavailable checks stay unresolved; Done requires its criteria and required review. User browser review and any explicitly requested agent browser checks follow AGENTS.md. No testing/delegation is started by this draft.

Implementation Plan is approved; stop for review of refined Tickets.md. Only after that review and explicit implementation authorization may one eligible ticket become Ready/start. No coding, installation, database/key provisioning, tests, or automatic continuation is authorized by this refinement.
