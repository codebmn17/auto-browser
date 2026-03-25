# Roadmap

This is the near-term direction for Auto Browser.

## Now (shipped in v0.5.0)

- stable local-first browser control
- reusable auth profiles + import/export
- human takeover via noVNC
- approvals and audit trails
- MCP transport + REST API with 30+ tools
- Docker-based isolated session mode
- CDP connect mode — attach to an existing Chrome
- Network inspector — request/response capture with PII scrubbing
- PII scrubbing layer — pixel redaction, console, network (16 pattern classes)
- Proxy partitioning — named proxy personas for per-agent IPs
- Shadow browsing — flip headless → headed for live debugging
- Session forking — clone auth state into a new branch session
- Playwright script export — session replay as runnable .py
- Shared session links — HMAC-signed TTL observer tokens
- Vision-grounded targeting — Claude Vision element identification
- Cron + webhook triggers — autonomous scheduled jobs
- MCP Resources Protocol — live browser state as subscribable resources
- Operator dashboard at `/ui/` with SSE event stream

## Next

- better session recovery and resume flows (crash-tolerant agents)
- cleaner multi-tab / popup management
- easier auth profile setup wizard
- improved docs and example workflows
- hosted demo environments for contributors
- resource subscription push (MCP `resources/subscribe` + `notifications/resources/updated`)
- stronger trace viewer integration in operator dashboard

## Later

- richer workflow recipes and app-specific helpers
- hosted control plane
- enterprise deployment support
- stronger remote access ergonomics
- session recording / replay with step-level time travel

## Explicit non-goals

Auto Browser is not being built as:
- a stealth browser
- an anti-bot bypass tool
- a CAPTCHA solver
- an unauthorized scraping framework

## Product direction

The open-source core should be excellent on its own.

If the project commercializes later, the likely path is:
- hosted runners
- managed auth/session storage
- team features
- enterprise deployment/support
