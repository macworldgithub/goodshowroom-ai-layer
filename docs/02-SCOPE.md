# Good Showroom AI Chatbot - Project Scope

Purpose: Define the first release using the approved PRD and inspected operational data.

Status: Approved by User  
Updated: 2026-10-22  
Basis: `01-PRD.md`. The user approved Scope and User Flows on 2026-10-22 and authorized drafting Architecture. Implementation is not started by this approval.

Change record (2026-10-22): Explicit user-directed architecture amendments add opt-in native developer tracing and application-owned atomic chat commits, and retain current credentials with application-level read-only protection. These technical changes are defined in [Architecture](04-ARCHITECTURE.md); the question families and release boundaries remain approved.

## 1. Release Boundary

Build an independent, local-development AI chatbot in `goodshowroom-ai-layer/`. Read existing operational MongoDB data through controlled backend tools. Use the three cloned backends to understand data, without changing/running them or depending on their APIs. Keep operational records unchanged.

## 2. Included Capabilities

| Area | Included work | PRD |
|---|---|---|
| Identity | Google login through NextAuth; server-verified identity in FastAPI; no business access from login alone. | 01 |
| Approval | Access request/status screens; admin approval/rejection, email preapproval, revocation, and access-change records. | 01, 09 |
| Administrators | Bootstrap one trusted configured Google email; support more administrators granted by existing admins. Prevent self-approval and accidental loss of the last active admin. | 01, 09 |
| Permissions | Admin-defined roles; database/domain, dealership/site, assigned/selected records, and field restrictions. Enforce before every read, count, response, and saved-data display. | 09, 10 |
| Questions | One agent with typed, bounded operational reads; MetricRegistry definitions and deterministic calculations; clarification and honest unavailable/unsupported results. | 02-04 |
| Responses | Evidence-backed ResponseBuilder for facts/text/data and matching inline visuals, with source, period, timezone, filters, freshness, coverage, and warnings. | 05, 11 |
| Saved chat | Application-owned ChatRepository atomically commits validated turns after access checks; bounded authorized context and safe follow-ups. No automatic SDK permanent-history writes. | 03, 06 |
| History review | Users access their own saved chats. Administrators can review all saved messages; disclose review and record access. Review does not bypass operational-data grants or let admins impersonate the owner. | 06, 10 |
| Temporary Chat | Active-chat context only; no saved transcript, summary, tool evidence, or visual for later user/admin review. Discard on chat end; no content in logs/tracing/analytics. | 03, 06, 07 |
| Interface | Login/status, chat with session selection and Temporary Chat, admin access settings, and saved-history review. Responsive loading/error/denial states. | 07 |

AI-layer writes are limited to application records such as approved access settings, saved conversations, and minimal audits. This is compatible with read-only business operations. Temporary messages bypass app persistence. Provider-side retention is separate and must be disclosed accurately.

Native OpenAI developer tracing is opt-in, separate from chat history, and contains permitted diagnostic metadata only by default. Temporary turns may produce such metadata when tracing is enabled, never a saved transcript. Application administrators do not automatically have developer dashboard access. See Architecture section 9 for the single authoritative tracing policy.

## 3. First-release Question Families

Examples describe supported question families, not tickets or fixed user roles. Admin grants determine eligibility; an admin title alone does not grant every operational dataset.

| ID | Example | Data and access needed | Output |
|---|---|---|---|
| Q-01 | How many leads did we receive in the last seven days? | Permitted leads; verified source-date rules. First vertical milestone. | Metric card |
| Q-02 | How many leads arrived today? Compare this month with last month. | Same rules; explicit date ranges and dealership timezone. | Cards/comparison chart |
| Q-03 | Break down leads by status or source for that period. | Approved source/status mapping; filters before aggregation. | Bar chart/table |
| Q-04 | Show my assigned or pending leads. | Verified Google-user-to-staff mapping; agreed meaning of pending; permitted fields only. Ask for clarification when needed. | Paginated table |
| Q-05 | Compare leads across my permitted dealerships or employees. | Verified site/employee mapping; permission to compare each group. | Bar chart/table |

Unknown mappings are handled inside the flow: clarify, show a permitted unassigned category when appropriate, or explain that the question is not yet supported. They do not block the whole project. Questions must never count unapproved records or silently omit uncertain data.

Conditional extensions, outside the initial question set: pending deliveries/customer delivery status, converted leads with linked sales, inventory, and contractor questions. Review their definitions and access before enabling them. No automatic expansion merely because a collection exists.

## 4. Date and Data Rules

- Last seven days means the previous seven completed dealership calendar days. Today means local midnight to now. Explicit rolling-hour requests use that rolling period.
- Use the verified dealership timezone; the source code currently defaults to `Australia/Melbourne`. State the timezone; convert query boundaries to UTC. Cross-timezone reporting still needs a selected rule.
- Prefer verified original lead creation time. Presence of `leadCreatedDate` does not prove its provenance; import code sometimes substitutes scrape/test-drive dates. Do not silently use these as received dates.
- Counts must state uncertain-date coverage and applicable exclusions. Do not use `receivedDaysAgo` as the reporting clock or old API fallback numbers.
- Normalize mixed date/ID formats in read adapters without updating source documents. Ambiguous/missing staff/site values must never widen access.

## 5. Live Database Discovery Evidence

Read-only inspection completed on 2026-10-22 using the supplied `.env` settings. Both connections succeeded. Inventory and index metadata were read for 40 collections. Schema inference used the first **up to 100 documents per collection**, including one level of nested object fields; arrays and every possible field variant were not fully inspected. Separate lead coverage and selected relationship checks covered the listed collections. No credentials, customer details, or record IDs are included here.

The values below are collection metadata estimates at inspection time, not validated reporting metrics.

| Database | Selected collection | Estimated records | Observed structure/use |
|---|---|---:|---|
| `byd-leads-new` | `leads` | 11,960 | BSON `_id`; source IDs; `platform`, `status`, `stage`, `dealer`/`dealerName`, staff-name fields, `leadCreatedDate`, `leadDate`, `createdAt`. |
| `byd-leads-new` | `appointments` | 1,478 | String lead/source links; `when` is a string in the sample; `testDriveDate` is a date/null. |
| `byd-leads-new` | `inventories` | 939 | Stock/identifier/status and nested vehicle specifications. |
| `byd-leads-new` | `users`, `staff_site_memberships` | 80 / 67 | Staff identity and site arrays; source-directory data is not chatbot approval. |
| `byd-panel` | `clients` | 3,378 | Delivery `id` is a string; stage/site/agent links; date fields include strings, BSON dates, and nulls. |
| `byd-panel` | `customers`, `opportunities` | 356 / 354 | String business IDs and site/owner fields; delivery/sales links; staff IDs are often absent/null in samples. |
| `byd-panel` | `saleslogentries` | 352 | Sale, opportunity, customer, and delivery links. |
| `byd-panel` | `virtual_yard_deals` | 7,656 | Imported deal, vehicle, status, date, and monetary fields. |
| `byd-panel` | `users`, `staff_site_memberships` | 116 / 67 | Delivery staff and site-directory structure. |

Other inspected collections: lead conversations/audits/settings/dealerships; delivery allocations, stock holds, targets, timelines, inspections, messages, offers/snapshots, document records, templates, settings/audits, sharing/upload records, and contractor jobs/evidence/time/activity records. Some collections were empty. Existence does not authorize exposure of tokens, passwords, documents, or message content.

**Verified limits and response:**

- The full lead scan counted 8,586 Virtual Yard, 3,369 Dealer Studio, 2 Autogate, and 3 manual records. Factual correction during Schema review (2026-10-22): the current lead model also includes `dealer_studio`; the earlier note that its enum omitted this value was incorrect. Adapters must support the verified live variant; no source-model change is required.
- `leadCreatedDate` is a BSON date in 11,783 of 11,960 records. It is absent/null in 174 Virtual Yard and 3 manual records. Even populated dates need provenance checks. Coverage must be reported rather than invented.
- All 354 opportunity customer references matched existing customer IDs. Of 351 populated opportunity delivery references, 349 matched client IDs; all 351 populated sales-log references matched. Customer delivery links also matched 349 of 351 populated references. Matching IDs do not by themselves prove business meaning.
- Source staff-name fields cannot safely replace verified account-to-employee IDs. Membership data can inform mapping; it cannot automatically approve a chatbot account.
- Lead indexes include staff/status/source-ID lookups but no observed index on `leadCreatedDate` or `leadDate`. Index inventory is verified; response-time targets/query plans are not. Add no source indexes during discovery.
- Both operational credentials report `readWriteAnyDatabase`. The user explicitly requires keeping credentials/permissions unchanged. OperationalReadRepository and trusted query builders enforce reads in the application; app writes target only `byd_ai_layer`. This is not database-enforced protection if code is bypassed or credentials are compromised; no credential change is a prerequisite.

## 6. Excluded Work

No business updates, bookings, SMS/email, imports, workflow execution, arbitrary model-generated Mongo queries/code, cross-user memory sharing, downloadable reports, or changes to sibling repos. No new accounting, workshop, supplier, marketing, or Pentana capabilities. No deployment, cloud provisioning, Docker/Kubernetes, CI/CD, or external observability infrastructure. Native OpenAI developer tracing is explicitly allowed. Do not add external integrations or dependencies merely to resolve an unsupported question.

## 7. Release Evidence and Remaining Choices

Completion requires verified identity/approval; role and session isolation; accurate permitted lead answers/visuals; saved-context follow-ups; administrator history review; and temporary-context non-persistence through errors/retries. Test with isolated fixtures/adapters and a separate test database as detailed later. Never perform operational writes to test a read-only restriction. Live schema inspection is not application testing.

Remaining design choices: exact trusted admin email at login setup, account/session verification contract, role precedence/mappings, trustworthy source-date semantics, saved-chat retention/deletion, temporary-context expiry/provider disclosure, result/context budgets, and UI/response contracts. Resolve these in the relevant design work; do not require extra credentials or Atlas setup before starting local project work.

Scope and User Flows are approved. Architecture is the current document for review. The user explicitly authorized Backlog tickets for these architecture requirements; no implementation or next-document phase starts automatically.
