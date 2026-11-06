# Instructions for AI Agents

## Workflow

- Read relevant approved documents and `docs/DECISIONS.md` before changing code. Placeholders marked `Not Started` are not approved requirements.
- Treat `Tickets.md` as the task register. Work on one approved ticket at a time. Check its dependencies and acceptance criteria first. Follow existing conventions and keep work within its scope.
- Complete the full authorized ticket before stopping: finish every acceptance criterion, required implementation, test and documentation update. Continue through partial changes, passing focused tests and intermediate milestones; do not stop merely because progress was made. Stop with unfinished work only when the user directs it, required authorization or information is missing, or a technical blocker prevents further progress. Complete independent authorized work first, then report the exact remaining work and blocker.
- Update `Tickets.md` and `planning/PROGRESS.md` with verified facts. Mark a ticket Done only after its acceptance criteria are met and required review is complete. Wait for authorization before starting the next ticket.
- Draft one project document at a time in the agreed order. Present it for review and wait for explicit approval before starting the next document.
- Obtain approval before deployment or destructive operations. After a push, verify that the intended commit is on the remote branch and report the commit ID and remaining local changes.

## Implementation and security

- Prefer small, readable changes and existing patterns. Avoid unrelated changes, unnecessary dependencies and speculative features.
- Treat external input and database output as untrusted. Validate inputs and encode outputs for their context. Enforce authorization and tenant or record scope at protected boundaries; never assume a request is authenticated.
- Keep credentials, tokens and mock passwords out of code and logs. Use environment configuration and placeholders. Use established, pinned dependencies and review their risks.
- Review changed code for relevant security threats and explain the protections and any remaining risks in the completion report.

## Tests and verification

- Use focused checks during development. Wait until edits are complete before final verification, then run the required checks on the final file state. Repeat broad checks only when later changes, failures or unresolved concerns justify it.
- Use synthetic data and isolated test services by default. Verify the target before any test that can write data. Distinguish simulated evidence from live integration results; leave criteria unchecked when required evidence is missing.
- The user normally tests the browser. When the user requests browser testing, delegate it to GPT-6.1 Terra with low reasoning and give clear steps. Use Playwright MCP; if it cannot perform the check, report the failure and use an appropriate isolated fallback. Guide the user through any remaining browser review.
- Report actual check results and material failed or repaired attempts. Never claim an unrun or failed check passed.

## Completion report

- Link and explain every added, changed or deleted project file, including its purpose and how it works. Group generated files only when each file is named.
- Give the commands run, results, remaining limits and steps the user can follow to verify the change. For website review, include the local URL, actions to try and expected results; distinguish completed checks from user review.
- State the next eligible ticket and why it comes next. Claim PRD completion only when all approved release requirements, ticket criteria, verification and required reviews are complete.
