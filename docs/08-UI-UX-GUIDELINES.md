# Good Showroom AI Chatbot - UI/UX Guidelines

Purpose: Define a clear chat interface, trustworthy data displays, and accessible employee/admin journeys.

Status: Approved by User  
Updated: 2026-10-22  
Approval: The user approved UI/UX Guidelines and the recommended libraries on 2026-10-22 and authorized the Security draft only. No coding is authorized.
Basis: Approved [PRD](01-PRD.md), [Scope](02-SCOPE.md), [User Flows](03-USER-FLOWS.md), [Architecture](04-ARCHITECTURE.md), [Tech Stack](05-TECH-STACK.md), [Database Schema](06-DATABASE-SCHEMA.md), and [API Contracts](07-API-CONTRACTS.md). Library/layout choices below are approved designs; package compatibility remains unverified. No UI code or packages were created.

## 1. Recommended UI Tools

| Need | Recommendation | Use |
|---|---|---|
| Interface components | shadcn/ui with existing Tailwind CSS | Buttons, inputs, cards, badges, sidebar/sheet, dialogs, alerts, and semantic tables. Add only used components; maintain their copied source in this repo. |
| Advanced tables | TanStack Table with shadcn Table | Admin lists and record tables needing controlled columns, sorting, and pagination. Simple result tables need only semantic markup. |
| Chat formatting | react-markdown; remark-gfm when needed | Headings, lists, emphasis, and safe links. GFM adds table formatting; authoritative business tables still come from structured datasets. |
| Charts | Approved Recharts with shadcn chart styling/components | Consistent bar/line charts and tooltips using validated backend data. |

TanStack Table supplies behavior, not visual styling or Markdown. Use server-controlled pagination/filtering/sorting; the library does not enforce permissions. Official patterns support [shadcn/TanStack composition](https://ui.shadcn.com/docs/components/base/data-table), [server-processed tables](https://tanstack.com/table/latest/docs/guide/client-side-vs-server-side), and [Recharts composition](https://ui.shadcn.com/docs/components/base/chart).

Keep React state and `fetch` as approved. No TanStack Query, Router, AI SDK, rich-text editor, or virtualization is needed for this version. Use one consistent official shadcn component base. Resolve compatible stable package versions during authorized implementation; do not copy version-specific examples blindly or change the backend agent framework.

## 2. Appearance and Layout

Use a light interface: off-white background, white surfaces, dark text, one blue accent, subtle borders, and restrained corner rounding. Use system fonts, approximately 16px body text, readable line spacing, and consistent spacing tokens. This styling direction is approved; final brand colors/logo and dark mode remain open.

- Desktop: left sidebar for new/saved chats; main conversation; composer at the bottom. Show the current mode and active permitted site/scope near the header. Put visual artifacts inside their answer, not in a separate dashboard.
- Mobile: collapse navigation into a sheet; stack cards/charts. Keep controls and composer clear of the keyboard/content. Wide tables scroll inside their own labeled region, not the entire page.
- Use short labels, plain-language errors, and safe generic chat titles. Show only granted navigation/actions; backend checks remain mandatory.

## 3. Screens and Main Actions

| Screen | Required behavior |
|---|---|
| Google login | Continue with Google; handle cancellation/failure. No public admin-claim control. |
| Access status | Request access once; show pending/rejected/revoked status and admin contact guidance. No protected previews or chat before approval. Preapproved users skip the request screen. |
| Chat | New saved chat, saved history, Temporary Chat, text composer, supported question suggestions, inline results, and sign-out. Suggestions reflect enabled metrics/grants; no unapproved conversion/delivery questions. |
| Access administration | Review requests; approve/reject; preapprove email; manage roles, narrowed employee scopes, verified staff mappings, and permitted settings. Preview effective access before saving. |
| Conversation review | Separate admin screen for saved messages, user/date selection, and dated visuals. Current grants still filter content; no impersonation or sending into the employee's chat. |

Use explicit site/record/field/metric selectors in access settings. Missing selections grant nothing. Explain that individual scope can only narrow the role. Confirm grant/revocation changes; show last-admin/self-approval validation errors. On stale edit, reload and show the current values instead of overwriting them. Admin settings writes affect only app access/configuration; business records remain read-only.

## 4. Chat Interaction and Result Anatomy

Composer: labeled multiline input, character count at the API limit, visible Send button; Enter sends and Shift+Enter adds a line, without sending during input-method composition. Disable duplicate submission while that chat has an active turn. Keep unsent/pending text in working memory only.

Show one complete validated answer; no token streaming, invented progress percentages, internal reasoning, or raw tool traces. Loading says "Preparing your answer." Keep users' scroll position when they read older messages; offer a new-answer/jump-to-latest control.

Each data answer presents:

1. A short conclusion with verified numbers.
2. A matching card, chart, or table where appropriate.
3. Period, dealership timezone, applied scope/filters, source, and retrieval time.
4. Visible coverage/suppression warnings; expandable safe evidence details.
5. A truthful saved/temporary status. Reopened results say they are dated snapshots; requesting fresh data creates a new turn.

Scope changes affect subsequent questions; they do not silently relabel old results. Clarify ambiguous periods/sites and missing staff mappings. Display actual resolved dates for "last seven days," which excludes today. Safe source labels reveal no credentials, raw queries, forbidden records, or internal trace links.

## 5. Cards, Charts, and Tables

| Backend result | Display |
|---|---|
| Single metric | Card with label, value, unit, and period. Verified zero displays `0`. |
| Category comparison | Bar chart with values/units and clear labels. |
| Ordered date trend | Line chart only with sufficient verified observations; missing values remain gaps, not invented zeroes. |
| Record list | Read-only semantic table; pagination and visible applied filters. |
| Missing/denied/unsupported/error | Clear status message; no fabricated number or chart. |

Render only approved structured visual kinds. Text and visual values use the same backend facts; the browser does not calculate business totals. Label partial/suppressed groups; never infer hidden values. Provide a text summary and accessible table alternative for charts. Use honest axes, distinguishable series labels, and no color-only meaning.

Honor API row/group bounds. Admin list controls use authorized cursors; omit unknown total/page counts and disable Next when no next cursor exists. Chat-table "Next page" sends the approved continuation turn. New sort/filter requests go to the backend when supported; never present page-only filtering as a full-data search.

## 6. Saved and Temporary Privacy

Saved mode displays: **"Saved chats can be reviewed by administrators."** Label a turn Saved only after confirmed atomic commit. Confirm saved-chat deletion; keep cleanup-pending status until completion is confirmed.

Temporary mode has a persistent label and **End temporary chat** action. Explain: **"This chat is not saved in your history and cannot be reviewed later by an administrator. Your usual data permissions still apply."** Also disclose permitted metadata-only developer diagnostics and separate provider handling; do not promise zero provider retention.

Switching modes starts a separate chat. Temporary content never enters saved history, persistent browser storage, URLs, analytics, or content logs. End/logout/auth expiry/restart clears the display and invalidates context; refresh cannot restore it. Disclose the API's 30-minute idle expiry and failed-cleanup lease bounds without claiming immediate remote deletion when unconfirmed. Late replies cannot reopen an ended chat. No employee/admin trace-inspection screen exists.

## 7. Loading, Error, and Recovery States

| State | Interface behavior |
|---|---|
| Complete/partial | Show checked result; make partial coverage warning visible beside it. |
| Clarification | Ask one focused question; no guessed result. |
| Unsupported/unavailable | Explain the supported boundary or missing evidence. |
| Access/session lost | Remove protected current output, clear temporary state, and show access status/sign-in. |
| Source/model failure | Safe error and retry guidance; no fallback numbers. |
| Save failed/unknown | Say "Not saved" only when confirmed; otherwise "Save status unknown." Check request status before offering a safe retry. |
| Conflict/rate limit | Reload versions or wait as instructed; preserve request identity for recovery. Never silently repeat paid work. |

Clear protected in-memory views on logout/account changes or revoked/narrowed grants. UI hiding is not authorization and cannot recall data already seen. Use persistent inline errors for failures needing action; brief success notices may use a toast. No operational edit buttons, exports, attachments, or unapproved actions.

## 8. Accessibility and Review

Target [WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/): keyboard navigation, visible/unobscured focus, labeled fields, meaningful headings, sufficient text/control contrast, and text alongside status colors. Prefer 44px touch targets. Dialogs manage/restore focus; status changes use polite announcements. Support text zoom, reduced motion, mobile reflow, and keyboard-accessible chart/table alternatives. Library components alone do not prove compliance.

Restricted Markdown is an approved presentation refinement for the API's existing `answer` string, with no payload change. Disable raw HTML/MDX, embedded images/iframes, executable content, and unsafe URLs. Keep a restricted element/plugin allowlist and safe link policy; never weaken URL handling. See [react-markdown security](https://github.com/remarkjs/react-markdown#security) and [remark-gfm](https://github.com/remarkjs/remark-gfm). Structured facts remain authoritative even when prose uses Markdown.

Library/layout recommendations are approved; final branding remains open. Retention, disclosure thresholds, and technical security details remain with their existing documents. Later implementation verification must cover keyboard/mobile states, accessible visuals, permission changes, retries, safe Markdown, and temporary non-persistence. No browser/application tests ran here.

[Tickets.md](../Tickets.md) remains the only implementation register; no tickets were started or new UI tasks generated. Security is approved; Test Strategy is the current authorized draft. Stop for its review before the next document; do not install packages or start coding yet.
