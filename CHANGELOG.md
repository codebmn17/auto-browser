# Changelog

All notable changes to auto-browser are documented here.

## [0.3.0] — 2026-03-18

### Added

#### Perception Presets
Three observe modes via `preset` query param or `POST /sessions/{id}/observe` body:
- **`fast`** — screenshot only; skips OCR and accessibility tree. Sub-200ms observe loops for tight agent feedback cycles.
- **`normal`** — current default. Screenshot + OCR + accessibility tree + interactables.
- **`rich`** — normal with doubled interactable limit and 4000-char text excerpt for complex pages.

New `POST /sessions/{id}/observe` endpoint accepts `{preset, limit}` body for richer control.
Config: `PERCEPTION_PRESET_DEFAULT` (default: `normal`).

#### SSE Event Stream
`GET /sessions/{id}/events` — Server-Sent Events stream for live session monitoring.
- Events: `observe`, `action`, `approval`, `session`
- Keepalive comments sent every `SSE_KEEPALIVE_SECONDS` (default: 15s) to prevent proxy timeouts
- Global subscriber support for multi-session dashboards

#### Screenshot Diff
`POST /sessions/{id}/screenshot/compare` — pixel-by-pixel diff against the most recent prior screenshot.
Returns `changed_pixels`, `changed_pct`, diff image URL, and source image URLs.
Useful for verifying that an action had visible effect.

#### Approval Webhooks
Set `APPROVAL_WEBHOOK_URL` to receive a signed POST whenever an approval is created.
- Payload: `{event, approval_id, session_id, kind, status, reason, created_at, updated_at}`
- Signature: `X-Webhook-Signature: sha256=<hmac>` (Slack-compatible)
- Secret: `APPROVAL_WEBHOOK_SECRET`

#### Auth Profile Export / Import
- `GET /auth-profiles/{name}/export` — downloads the named auth profile as a `.tar.gz` archive
- `POST /auth-profiles/import` — imports a `.tar.gz` archive into the auth root (supports `overwrite` flag)

#### Operator Dashboard
`/ui/` — dark-mode single-page operator dashboard served as static HTML.
- Session list with live status
- Screenshot panel with auto-refresh on SSE observe events
- SSE event log (newest first, capped at 200 entries)
- Pending approvals queue with one-click approve/reject
- Perception preset selector
- Screenshot diff button with pixel change readout

#### Python Client SDK
New `client/` package: `auto-browser-client` on PyPI (installable as `pip install auto-browser-client`).
- Sync and async variants for all core endpoints
- `stream_events()` generator for SSE
- `AutoBrowserError` with status code and detail

### Changed
- `GET /sessions/{id}/observe` now accepts optional `preset` query param (default: `normal`)
- `_observation_payload` returns a `preset` field indicating which mode was used
- `_page_summary` now accepts `text_limit` parameter (used by `rich` preset)
- Version bumped to `0.3.0` in FastAPI app and MCP transport

### Fixed
- `_write_tar` and `_compute_diff` are pure static methods — no BrowserManager instantiation needed for offline testing

## [0.2.0] — 2026-03-15

### Added
- 6 new REST endpoints: hover, select-option, wait, reload, go-back, go-forward
- ruff CI linting job
- 9 new unit tests
- `.env.example` improvements

## [0.1.0] — Initial release

- Playwright-based browser controller
- MCP JSON-RPC transport
- Agent step/run with OpenAI, Claude, Gemini
- Approval workflow (upload/post/payment/destructive)
- Auth profile management
- noVNC human takeover
- Docker isolation mode
- Social actions (post, comment, like, follow, dm)
- Audit log + metrics
