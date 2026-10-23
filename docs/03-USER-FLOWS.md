# Good Showroom AI Chatbot - User Flows

Purpose: Describe login, access, read-only questions, saved history, and Temporary Chat.

Status: Approved by User  
Updated: 2026-10-22  
Basis: Approved `01-PRD.md` and `02-SCOPE.md`. The user approved Scope and User Flows on 2026-10-22 and authorized drafting Architecture. Implementation is not started by this approval.

Change record (2026-10-22): The user's architecture revision changes saved-turn commit timing and clarifies temporary/logout and diagnostic-tracing boundaries below. Other approved user journeys remain intact; implementation details live in [Architecture](04-ARCHITECTURE.md).

## 1. Common Access Rules

Every protected action verifies identity, active access, and current permissions on the server. A browser role, session ID, or prompt cannot grant access. Database reads apply site/record/field restrictions before returning or counting data. No action changes operational records.

Users can reopen their own saved chats. Administrators can review all saved messages; employees cannot view one another's chats. Protected operational facts and saved visuals still follow the reviewer's current data grants. Temporary transcripts are not available for later review by anyone.

## 2. Google Login and Access Request

1. The visitor selects **Continue with Google**. NextAuth completes Google authentication; the backend verifies the trusted identity.
2. The system checks the verified provider account against the AI-layer access register.
3. An active approved user enters chat. A matching preapproved email with role/scope is bound to the verified Google account and enters chat without a request screen.
4. A new user sees **Request access** and submits a request. It enters Pending; repeated submissions do not create duplicate active requests.
5. Pending users see their request status. Rejected/revoked users see a status screen without protected data. The interface states how to contact an administrator; resubmission rules will be defined later.
6. After approval, refresh/reverification opens chat with the assigned scope. Show the saved-chat administrator-review notice and the Temporary Chat option.

Google cancellation/failure returns to login. Session expiry requires sign-in again. A matching display email alone is insufficient; exact account-ownership/session rules are defined in the security design.

## 3. First and Additional Administrators

1. Before first-admin login, configure one trusted Google email. This is the only user-provided value still needed for that setup; it does not block drafting/local work.
2. The verified matching Google account receives the initial administrator role through a one-time setup path. Ordinary users cannot claim it.
3. An existing administrator can approve/preapprove further administrators. Administrator creation is a deliberate access-setting action, not inferred from source-system roles.
4. Preserve at least one active administrator; reject removal/revocation of the last active administrator.

Administrator settings access and operational data access are distinct. A new administrator receives explicit operational scope rather than automatic unrestricted data access.

## 4. Administrator Manages Employee Access

1. Open access settings and view pending requests or select **Preapprove email**.
2. Create/select a role. Choose permitted domains/databases, dealerships/sites, assigned/selected records, and fields.
3. Preview the resulting grant and approve, reject, or save the preapproval. Admin changes write only AI-layer access records and audits.
4. On the user's next protected action, enforce the latest grant. Missing employee mappings do not widen access; keep affected assigned-record questions unavailable or clarify scope.
5. To change/revoke access, edit the grant. Check in-flight responses and reopened history against the new grant; remove protected output that is no longer allowed.

Reject non-admin settings requests and ordinary self-approval. Show validation errors before saving an incomplete role/scope. Exact role/individual precedence remains a later design choice.

## 5. Ask a Read-only Question

1. The user opens a saved chat or starts a new one, then asks a supported question such as **How many leads did we receive in the last seven days?**
2. Verify the user/session and permissions. Resolve the metric, allowed site/record scope, dates, and timezone.
3. If the question is ambiguous, ask one focused clarification before querying. For "my leads," require the verified staff mapping; for "pending," require the agreed lifecycle meaning.
4. Run a typed read tool with validated filters, safe field selection, and bounded results. Calculate metrics in code. Do not execute free-form model-generated database queries.
5. ResponseBuilder validates evidence and constructs readable text, authoritative facts/data, and an approved visual. Recheck current permissions; do not stream unvalidated output.
6. In saved mode, ChatRepository atomically commits the validated user/assistant turn, response, state, and permitted evidence before reporting save success. The SDK never automatically writes intermediate items to permanent history. Temporary mode updates memory only. Recheck access before release.
7. Display the reporting period, timezone, source/scope, freshness, and limitations. A single count uses a card; groups use a bar chart; ordered trends use a line chart; records use a paginated table. Report persistence failure accurately. Never mutate source records.

**Example:** "Last seven days" ends at today's local midnight and excludes today. Records without a trustworthy original source date are disclosed as a coverage limitation. The system does not substitute import/test-drive dates or label unavailable data as zero.

## 6. Follow-up and Resume

1. The user asks **What about last month?** in the same chat.
2. Load only that session's authorized context; preserve relevant site/source filters and replace the reporting period.
3. Use bounded recent turns and validated question state; summarize older turns only when needed. Retain complete tool-call/result pairs.
4. Fetch fresh data through the same access checks. A previous answer/summary is not evidence of current counts.
5. Save the result only in saved mode. On reopening, show saved visuals as dated snapshots and recheck access; request fresh data for a current answer.

A new chat starts without another chat's memory. When access changes, rebuild/omit unsafe history and summaries. If summary generation or context recovery fails, use safe recent context and ask for the missing detail. Failed requests must not create duplicate saved turns or stale concurrent state.

## 7. Temporary Chat

1. Select **Temporary Chat** before sending the first message. Show: **This chat is not saved in your history and cannot be reviewed later by an administrator. Your usual data permissions still apply. Developer diagnostics may retain permitted metadata, but not the conversation.** Explain provider handling separately; never expose trace contents/links in chat.
2. Create a separate temporary session. Do not convert or copy an existing saved chat into it automatically.
3. Keep context only for the active chat. Apply the same identity, role, query, source-coverage, and rendering checks as saved chat.
4. Show answers and visuals in the open chat. Do not persist temporary messages, summaries, filters/state, tool evidence, or visuals in MongoDB, browser persistent storage, content logs, tracing, or analytics. Do not silently fall back to saving when an error occurs.
5. On **End temporary chat**, sign-out, or session expiry, invalidate the login-bound temporary session, cancel in-flight work, discard context, and clear browser state. Logout coordinates protected backend cleanup/session invalidation with NextAuth sign-out. If delivery fails, deny reuse and apply bounded lease/idle cleanup as defined in Architecture; tab-close cleanup alone is insufficient. Backend restart loses all temporary context. Reload/late results must not resurrect it.
6. Temporary chats never appear in user history, administrator review, search, exports, or later agent context. Minimal security events contain no message content.

Saved mode and temporary mode remain separate; switching starts another chat. Exact lifecycle/storage/provider settings are defined in Architecture and Security. Do not claim that an external AI provider has zero retention without verified configuration.

When explicitly enabled, native OpenAI developer traces can retain safe stage/outcome/timing metadata for temporary turns. Sensitive chat/tool content stays excluded. Model `store=false` and trace export are separate controls. Only authorized developers can inspect traces in the OpenAI project; employee/admin app screens expose no inspection route.

## 8. Administrator Reviews Saved Conversations

1. An active administrator opens **Conversation review**, selects a user, and selects a saved session.
2. Verify administrator access and record the review event without duplicating transcript content into the audit.
3. Show the saved messages. Apply current operational grants to protected answer/tool data and visuals; redact restricted facts instead of using review access to bypass data permissions.
4. Do not run a query as the employee, alter the employee's session, or feed the reviewed transcript into the administrator's agent context automatically.
5. Temporary chats are absent. Revoked administrator access closes review on the next protected action.

Employees see a clear notice that saved conversations can be read by administrators. The UI distinguishes administrator review from the administrator's own chat history.

## 9. Common Outcomes

| Situation | User-visible behavior |
|---|---|
| No data permission | "You do not have access to this information. Please contact an administrator." Do not expose internal database names or restricted existence/counts. |
| Some requested sites forbidden | Ask to use an allowed scope or deny the forbidden scope. Never silently present a partial authorized count as the requested total. |
| Verified zero results | Display zero with the applied period/scope; no invented trend points. |
| Missing dates, unmatched links, incomplete results | Explain the limit; label known coverage. Clarify or decline the unsupported metric rather than fabricate it. |
| Unsupported question or business write request | Explain available read-only capabilities. Do not update records, send messages, or execute workflows. |
| Database/model timeout or failure | Show a retryable error; no fallback business numbers. Retry without duplicate turns. |
| App-history save failure | Report that the answer was not saved; do not claim durable history. Temporary mode stays unsaved. |
| Expired/revoked access | Require login or show access status; do not release protected in-flight content. |
| Prompt injection in user/retrieved text | Treat retrieved text as data. Reject attempts to change policy, reveal secrets, or run arbitrary code/queries. |

## 10. Review Boundaries

These flows cover the five approved question families in Scope. Delivery/conversion/contractor flows require later scope review. Screen layouts, API shapes, database schema, retention durations, and context/response limits remain for their relevant documents. MongoDB credentials/permissions remain unchanged; operational reads are enforced in the application.

Scope and User Flows are approved. Architecture is the current document for review. The current user request explicitly authorizes architecture Backlog tickets, but no application features or next development phase.
