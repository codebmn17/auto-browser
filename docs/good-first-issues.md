# Good first issues

Use these as public contributor tickets that match the current v1.0.4 product surface.

## 1. Improve multi-tab and popup recovery tests

- **Label:** `good first issue`
- **Scope:** add fixtures and regression coverage for popups, tab switching, closed tabs, and returning to the useful active tab
- **Why it matters:** real browser workflows often branch into new tabs before the agent can finish cleanly

## 2. Add MCP resources and subscribe examples

- **Label:** `good first issue`
- **Scope:** document and test MCP resource listing, session/audit resources, and subscription-style update examples for clients that support them
- **Why it matters:** MCP users need more than one-shot tools once sessions run for several steps

## 3. Build an auth profile setup wizard

- **Label:** `enhancement`
- **Scope:** add a small dashboard flow for naming a profile, guiding manual login, saving auth state, and reopening a session from that profile
- **Why it matters:** auth reuse is the strongest demo, but the current path is still too curl-heavy for first-time operators

## 4. Add approval-focused eval fixtures

- **Label:** `good first issue`
- **Scope:** create local HTML fixtures for submit buttons, upload controls, and account-change flows, then wire them into `evals/agent_cases.json`
- **Why it matters:** governed mode is only credible when approval-required behavior is easy to reproduce

## 5. Add a lightweight replay view for agent runs

- **Label:** `enhancement`
- **Scope:** render checkpoints, actions, screenshots, approvals, and final session state from existing job/session artifacts
- **Why it matters:** operators need to debug what happened without reading JSONL files by hand

## 6. Raise controller coverage toward 80%

- **Label:** `good first issue`
- **Scope:** add focused tests for `BrowserManager`, startup extension wiring, network inspector paths, and route handlers without needing live browsers
- **Why it matters:** v1.0.4 adds a real coverage ratchet, but the historical global baseline is still below the desired 80% bar
