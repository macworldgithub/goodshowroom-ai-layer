# Instructions for AI Agents

## Task Completion Report

- Explain every added, changed, or deleted project file. Link to each file and state what changed, what it does, and how it works. Group generated files only when each file is still named.
- Report the checks actually run, their results, and any remaining limits. Give exact commands and expected results so the user can check the change.
- When website review is relevant, give the local URL and clear steps. Explain what the user should see and which actions to try. Distinguish checks you ran from checks left for the user.
- End with the next eligible ticket from Tickets.md and why it comes next. Wait for authorization before starting it.
- Claim the project is complete according to the PRD only after all approved release requirements, ticket acceptance criteria, verification, and required reviews are complete. Otherwise state what remains.

- Read the relevant project documentation before making changes. Placeholder documents marked `Not Started` are not approved requirements.
- Use `Tickets.md` as the authoritative task register. Work on one approved ticket at a time, and review its dependencies and acceptance criteria before implementation.
- Follow existing code conventions and approved decisions in `docs/DECISIONS.md`.
- Keep changes within the selected ticket. Avoid unrelated modifications, unnecessary dependencies, and speculative functionality.
- Write and run appropriate tests for the ticket. Use the Playwright MCP server when browser testing or verification is needed.
- Report test results accurately; never claim a test passed unless it ran successfully.
- Update ticket status and `planning/PROGRESS.md` only with verified information. Do not mark a ticket Done until its acceptance criteria are met and completion evidence is recorded.
- Obtain approval before deployment or destructive operations.
- Draft one project document at a time in the agreed order. Present it for review and wait for explicit approval before starting the next document. Keep decision and progress records consistent with verified user directions.
- Avoid overengineering: prefer small, readable changes and reuse existing patterns. Minimize code without sacrificing clarity or security. Validate untrusted input, enforce authorization, handle errors deliberately, and protect secrets. Avoid unnecessary dependencies, abstractions, and speculative features; use environment configuration for sensitive settings.

## Security Rules to Strictly Follow
1. SECURE DEFAULTS: Assume all external inputs (including database outputs, user parameters, and headers) are untrusted. Implement explicit, strict input validation and contextual output encoding.
2. ACCESS CONTROL: Every logic block, function, or endpoint you create must incorporate authorization and multi-tenant filters by default. Never assume a request is pre-authenticated.
3. SECRET MANAGEMENT: Do not include hard-coded secrets, tokens, or mock passwords under any circumstances. Use placeholders for environment variables.
4. DEPENDENCY CHECKING: Use only established, standard library packages or verified, long-standing third-party libraries. Pin specific, stable versions. Do not introduce speculative or arbitrary packages.
5. SELF-REVIEW: Before outputting the final code, perform a rigorous static analysis. Explicitly list any potential threat vectors or attack surfaces associated with this logic and document how you have neutralized them.

## Test Code Authoring

- Whenever test code must be created or changed, delegate that work to a sub-agent using **GPT-6 Luna** (`gpt-6-luna`) with reasoning effort **medium**. This applies to unit, integration, API, component, browser, and test-harness code.
- Give the sub-agent the approved ticket, relevant requirements, affected files, expected behavior, and verification commands. Review its changes and actual check results before reporting completion.
- If that model or reasoning setting is unavailable, report the limitation and ask the user before using a different configuration for test-code authoring.

## Browser Test

The User will test the Browser but if the user has ask you to test it you should launch a sub-agent using GPT-6 luna medium to test. And guide the user what to test and how to test the browser. and when you run the sub-agent you should give a detail step by step clear detail guidelines to the sub-agent.

Browser execution uses the existing medium setting. Creating or changing browser test code uses the max setting specified above.
