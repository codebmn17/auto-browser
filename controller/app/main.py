from __future__ import annotations

import asyncio
import hmac
import html as _html
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from . import events as _events
from .action_errors import BrowserActionError
from .app_factory import (
    build_controller_services,
    create_controller_app,
    install_controller_host_middleware,
)
from .approvals import ApprovalRequiredError
from .audit import reset_current_operator, set_current_operator
from .compliance import VALID_TEMPLATES, apply_compliance_template, write_compliance_manifest
from .config import get_settings
from .models import (
    ClickRequest,
    CreateSessionRequest,
    ExecuteActionRequest,
    HoverRequest,
    HumanTakeoverRequest,
    NavigateRequest,
    ObserveRequest,
    OpenTabRequest,
    PressRequest,
    ScreenshotRequest,
    ScrollRequest,
    SelectOptionRequest,
    TabIndexRequest,
    TypeRequest,
    UploadRequest,
    WaitRequest,
)
from .rate_limits import build_rate_limit_key, is_exempt_path
from .routes._utils import require_safe_segment
from .routes.agent import create_agent_router
from .routes.auth_profiles import create_auth_profiles_router
from .routes.mcp import create_mcp_router
from .routes.operations import create_operations_router
from .routes.share import create_share_router
from .runtime_policy import validate_runtime_policy

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(level=_log_level)
logger = logging.getLogger(__name__)

_VERSION = "1.0.6"

def _install_controller_host_middleware(application: FastAPI, allowed_hosts: list[str]) -> None:
    install_controller_host_middleware(application, allowed_hosts)


def _is_bearer_token_exempt_path(path: str) -> bool:
    return path in {
        "/healthz",
        "/readyz",
        "/mesh/receive",
        "/version",
        "/dashboard",
        "/dashboard/",
        "/ui",
        "/ui/",
    }

settings = get_settings()
_compliance_template = settings.compliance_template.upper().strip() if settings.compliance_template else None
_compliance_overrides: dict[str, object] | None = None
if _compliance_template:
    if _compliance_template not in VALID_TEMPLATES:
        raise RuntimeError(
            f"Invalid COMPLIANCE_TEMPLATE={_compliance_template!r}. Valid: {sorted(VALID_TEMPLATES)}"
        )
    _compliance_overrides = apply_compliance_template(settings, _compliance_template)
services = build_controller_services(settings, version=_VERSION)
proxy_store = services.proxy_store
manager = services.manager
providers = services.providers
orchestrator = services.orchestrator
job_queue = services.job_queue
cron_service = services.cron_service
share_manager = services.share_manager
vision_targeter = services.vision_targeter
tool_gateway = services.tool_gateway
rate_limiter = services.rate_limiter
metrics = services.metrics
maintenance = services.maintenance
mcp_transport = services.mcp_transport


@asynccontextmanager
async def lifespan(application: FastAPI):
    if _compliance_template and _compliance_overrides is not None:
        write_compliance_manifest(
            template_name=_compliance_template,
            overrides=_compliance_overrides,
            output_path=Path(settings.compliance_manifest_path),
        )
        logger.info("compliance template applied: %s", _compliance_template)
    policy_report = validate_runtime_policy(settings)
    if policy_report.errors:
        raise RuntimeError("Invalid runtime policy:\n- " + "\n- ".join(policy_report.errors))
    for warning in policy_report.warnings:
        logger.warning("runtime policy warning: %s", warning)
    await manager.startup()
    await job_queue.startup()
    await cron_service.startup()
    await maintenance.startup()
    try:
        from .startup.extensions import register_extensions

        register_extensions(application)
    except Exception as exc:
        logger.error("v1.0 extensions init failed (non-fatal): %s", exc)
    try:
        yield
    finally:
        await maintenance.shutdown()
        await cron_service.shutdown()
        await job_queue.shutdown()
        await manager.shutdown()


app = create_controller_app(services=services, version=_VERSION, lifespan=lifespan)
app.include_router(create_mcp_router(mcp_transport=mcp_transport, tool_gateway=tool_gateway))
app.include_router(create_agent_router(manager=manager, orchestrator=orchestrator, job_queue=job_queue))
app.include_router(create_auth_profiles_router(manager=manager, settings=settings))
app.include_router(create_share_router(manager=manager, share_manager=share_manager))
app.include_router(
    create_operations_router(
        manager=manager,
        proxy_store=proxy_store,
        cron_service=cron_service,
    )
)

# Legacy operator dashboard aliases now redirect to the auth-bootstrap-aware dashboard.
@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
@app.get("/ui/{rest_of_path:path}", include_in_schema=False)
async def legacy_ui_redirect(_: Request, rest_of_path: str = "") -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=307)


@app.exception_handler(KeyError)
async def handle_key_not_found(_: Request, exc: KeyError) -> JSONResponse:
    key = exc.args[0] if exc.args else "unknown"
    return JSONResponse(status_code=404, content={"detail": f"Not found: {key}"})


@app.exception_handler(BrowserActionError)
async def handle_browser_action_error(_: Request, exc: BrowserActionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload)


@app.middleware("http")
async def require_api_bearer_token(request: Request, call_next):
    path = request.url.path
    if not settings.api_bearer_token or _is_bearer_token_exempt_path(path):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.api_bearer_token}"
    if not hmac.compare_digest(header.encode(), expected.encode()):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


@app.middleware("http")
async def enforce_rate_limits(request: Request, call_next):
    if rate_limiter is None or is_exempt_path(request.url.path, settings.request_rate_limit_exempt_path_list):
        return await call_next(request)

    decision = await rate_limiter.evaluate(
        build_rate_limit_key(
            operator_id_header=settings.operator_id_header,
            headers=request.headers,
            client_host=request.client.host if request.client else None,
        )
    )
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after_seconds),
    }
    if decision.exceeded:
        headers["Retry-After"] = str(decision.retry_after_seconds or decision.reset_after_seconds)
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Rate limit exceeded",
                "limit": decision.limit,
                "window_seconds": decision.window_seconds,
                "retry_after_seconds": decision.retry_after_seconds or decision.reset_after_seconds,
            },
            headers=headers,
        )

    response = await call_next(request)
    response.headers.update(headers)
    return response


@app.middleware("http")
async def bind_operator_identity(request: Request, call_next):
    path = request.url.path
    exempt_prefixes = (
        "/healthz",
        "/readyz",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/artifacts",
        "/metrics",
        "/dashboard",
        "/ui",
        "/mesh/receive",
    )
    operator_id = request.headers.get(settings.operator_id_header)
    operator_name = request.headers.get(settings.operator_name_header)

    if settings.require_operator_id and not path.startswith(exempt_prefixes) and not operator_id:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Missing required operator header: {settings.operator_id_header}",
            },
        )

    token = set_current_operator(operator_id, name=operator_name, source="header")
    try:
        return await call_next(request)
    finally:
        reset_current_operator(token)


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    if not metrics.enabled:
        return await call_next(request)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - start
        metrics.record_http_request(
            method=request.method,
            path=request.url.path,
            status_code=500,
            duration_seconds=duration,
        )
        raise

    duration = time.perf_counter() - start
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    metrics.record_http_request(
        method=request.method,
        path=path,
        status_code=response.status_code,
        duration_seconds=duration,
    )
    return response


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return await manager.list_sessions()


@app.post("/sessions")
async def create_session(payload: CreateSessionRequest) -> dict:
    try:
        return await manager.create_session(
            name=payload.name,
            start_url=payload.start_url,
            storage_state_path=payload.storage_state_path,
            auth_profile=payload.auth_profile,
            memory_profile=payload.memory_profile,
            proxy_persona=payload.proxy_persona,
            request_proxy_server=payload.proxy_server,
            request_proxy_username=payload.proxy_username,
            request_proxy_password=payload.proxy_password,
            user_agent=payload.user_agent,
            protection_mode=payload.protection_mode,
            totp_secret=payload.totp_secret,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Conflict") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    return await manager.get_session_record(session_id)


@app.get("/sessions/{session_id}/observe")
async def observe(session_id: str, limit: int = 40, preset: str = "normal") -> dict:
    try:
        return await manager.observe(session_id, limit=limit, preset=preset)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown session") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/observe")
async def observe_post(session_id: str, payload: ObserveRequest) -> dict:
    """Observe with a perception preset. POST body allows richer options than query params."""
    try:
        return await manager.observe(session_id, limit=payload.limit, preset=payload.preset)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown session") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/screenshot")
async def capture_screenshot(session_id: str, payload: ScreenshotRequest) -> dict:
    return await manager.capture_screenshot(session_id, label=payload.label)


@app.get("/sessions/{session_id}/downloads")
async def list_downloads(session_id: str) -> list[dict]:
    return await manager.list_downloads(session_id)


@app.get("/sessions/{session_id}/tabs")
async def list_tabs(session_id: str) -> list[dict]:
    return await manager.list_tabs(session_id)


@app.post("/sessions/{session_id}/tabs/activate")
async def activate_tab(session_id: str, payload: TabIndexRequest) -> dict:
    try:
        return await manager.activate_tab(session_id, payload.index)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None


@app.post("/sessions/{session_id}/tabs/close")
async def close_tab(session_id: str, payload: TabIndexRequest) -> dict:
    try:
        return await manager.close_tab(session_id, payload.index)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None


@app.post("/sessions/{session_id}/tabs/open")
async def open_tab(session_id: str, payload: OpenTabRequest) -> dict:
    try:
        return await manager.open_tab(session_id, payload.url, payload.activate)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None


@app.post("/sessions/{session_id}/actions/navigate")
async def navigate(session_id: str, payload: NavigateRequest) -> dict:
    try:
        return await manager.navigate(session_id, payload.url)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/click")
async def click(session_id: str, payload: ClickRequest) -> dict:
    try:
        return await manager.click(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            x=payload.x,
            y=payload.y,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/type")
async def type_text(session_id: str, payload: TypeRequest) -> dict:
    try:
        return await manager.type(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            text=payload.text,
            clear_first=payload.clear_first,
            sensitive=payload.sensitive,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/press")
async def press_key(session_id: str, payload: PressRequest) -> dict:
    try:
        return await manager.press(session_id, payload.key)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/scroll")
async def scroll(session_id: str, payload: ScrollRequest) -> dict:
    try:
        return await manager.scroll(session_id, payload.delta_x, payload.delta_y)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/execute")
async def execute_action(session_id: str, payload: ExecuteActionRequest) -> dict:
    try:
        return await manager.execute_decision(
            session_id,
            payload.action,
            approval_id=payload.approval_id,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/upload")
async def upload(session_id: str, payload: UploadRequest) -> dict:
    try:
        return await manager.upload(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            file_path=payload.file_path,
            approved=payload.approved,
            approval_id=payload.approval_id,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/hover")
async def hover(session_id: str, payload: HoverRequest) -> dict:
    try:
        return await manager.hover(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            x=payload.x,
            y=payload.y,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/select-option")
async def select_option(session_id: str, payload: SelectOptionRequest) -> dict:
    try:
        return await manager.select_option(
            session_id,
            selector=payload.selector,
            element_id=payload.element_id,
            value=payload.value,
            label=payload.label,
            index=payload.index,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid request") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/wait")
async def wait(session_id: str, payload: WaitRequest) -> dict:
    try:
        return await manager.wait(session_id, payload.wait_ms)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown session") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/reload")
async def reload(session_id: str) -> dict:
    try:
        return await manager.reload(session_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/go-back")
async def go_back(session_id: str) -> dict:
    try:
        return await manager.go_back(session_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/actions/go-forward")
async def go_forward(session_id: str) -> dict:
    try:
        return await manager.go_forward(session_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not permitted") from None
    except ApprovalRequiredError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.post("/sessions/{session_id}/takeover")
async def request_human_takeover(session_id: str, payload: HumanTakeoverRequest) -> dict:
    return await manager.request_human_takeover(session_id, payload.reason)


@app.get("/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request):
    """SSE stream of observe/action/approval events for a session.

    Clients receive newline-delimited ``data: <json>\\n\\n`` messages.
    A keepalive comment is sent every ``settings.sse_keepalive_seconds`` seconds.
    """
    await manager.get_session(session_id)

    queue = _events.subscribe(session_id)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=settings.sse_keepalive_seconds,
                    )
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    # keepalive comment — prevents proxy from dropping idle connection
                    yield ": keepalive\n\n"
        finally:
            _events.unsubscribe(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/sessions/{session_id}/screenshot/compare")
async def screenshot_compare(session_id: str) -> dict:
    """Capture a screenshot and diff it against the most recent prior screenshot.

    Returns pixel change count, percentage, and a diff image URL.
    """
    try:
        return await manager.screenshot_diff(session_id)
    except Exception:
        raise HTTPException(status_code=500, detail="Internal error") from None


@app.get("/sessions/{session_id}/replay", response_class=HTMLResponse)
async def session_replay(session_id: str) -> HTMLResponse:
    """Session replay — HTML viewer showing screenshots, audit events, and approvals."""
    safe_session_id = require_safe_segment(session_id, field="session_id")

    # Gather screenshots, constrained to the artifact root.
    artifact_root = Path(settings.artifact_root).resolve()
    artifact_dir: Path | None = None
    if artifact_root.is_dir():
        for child in artifact_root.iterdir():
            if child.name == safe_session_id and child.is_dir():
                candidate = child.resolve()
                if candidate.is_relative_to(artifact_root):
                    artifact_dir = candidate
                break
    screenshots: list[tuple[str, str]] = []  # (url, label)
    if artifact_dir is not None:
        for f in sorted(artifact_dir.glob("*.png")):
            label = f.stem.replace("-", " ")
            screenshots.append((f"/artifacts/{safe_session_id}/{f.name}", label))

    # Gather audit events for this session
    try:
        events = await manager.list_audit_events(session_id=safe_session_id, limit=200)
    except Exception:
        events = []

    # Gather session info
    session_info: dict = {}
    try:
        session = manager.sessions.get(safe_session_id)
        if session:
            session_info = await manager._session_summary(session)
        else:
            record = await manager.session_store.get(safe_session_id)
            session_info = record.model_dump()
    except Exception:
        pass

    def esc(s: object) -> str:
        return _html.escape(str(s or ""))

    screenshots_html = "".join(
        f'<figure><img src="{esc(url)}" loading="lazy"><figcaption>{esc(lbl)}</figcaption></figure>'
        for url, lbl in screenshots
    ) or "<p class=muted>No screenshots captured yet.</p>"

    events_html = "".join(
        f'<tr><td class=muted>{esc(e.get("timestamp","")[:19])}</td>'
        f'<td>{esc(e.get("event_type",""))}</td>'
        f'<td>{esc(e.get("operator_id",""))}</td>'
        f'<td>{esc(str(e.get("data",""))[:120])}</td></tr>'
        for e in events
    ) or '<tr><td colspan=4 class=muted>No audit events.</td></tr>'

    status = esc(session_info.get("status", "unknown"))
    current_url = esc(session_info.get("url", ""))
    title = esc(session_info.get("title", session_id))
    created = esc(str(session_info.get("created_at", ""))[:19])

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replay — {esc(session_id)}</title>
<style>
  :root {{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;padding:24px}}
  h1{{font-size:18px;font-weight:600;margin-bottom:4px}}
  h2{{font-size:14px;font-weight:600;margin:24px 0 12px;border-bottom:1px solid var(--border);padding-bottom:6px}}
  .meta{{color:var(--muted);font-size:12px;margin-bottom:20px}}
  .meta span{{margin-right:16px}}
  .gallery{{display:flex;flex-wrap:wrap;gap:12px}}
  figure{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;max-width:340px}}
  figure img{{width:100%;display:block}}
  figcaption{{font-size:11px;color:var(--muted);padding:6px 8px}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border);font-weight:500}}
  td{{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top;word-break:break-word}}
  .muted{{color:var(--muted)}}
  a{{color:var(--accent);text-decoration:none}}
</style>
</head>
<body>
<h1>Session Replay</h1>
<div class="meta">
  <span>ID: <strong>{esc(safe_session_id)}</strong></span>
  <span>Status: <strong>{status}</strong></span>
  <span>Created: {created}</span>
  <span>Title: {title}</span>
  {f'<span>URL: <a href="{current_url}" target="_blank">{current_url}</a></span>' if current_url else ''}
</div>
<h2>Screenshots ({len(screenshots)})</h2>
<div class="gallery">{screenshots_html}</div>
<h2>Audit Events ({len(events)})</h2>
<table>
  <thead><tr><th>Time</th><th>Type</th><th>Operator</th><th>Data</th></tr></thead>
  <tbody>{events_html}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=body)


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str) -> dict:
    return await manager.close_session(session_id)


# ── Extended browser endpoints ──────────────────────────────────────────────

@app.get("/sessions/{session_id}/network-log")
async def get_network_log(
    session_id: str,
    limit: int = 100,
    method: str | None = None,
    url_contains: str | None = None,
) -> dict:
    return await manager.get_network_log(
        session_id, limit=limit, method=method, url_contains=url_contains
    )


@app.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, name: str | None = None, start_url: str | None = None) -> dict:
    try:
        return await manager.fork_session(session_id, name=name, start_url=start_url)
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Conflict") from None


@app.post("/sessions/{session_id}/shadow-browse")
async def enable_shadow_browse(session_id: str) -> dict:
    """Launch a headed browser session for debugging."""
    try:
        return await manager.enable_shadow_browse(session_id)
    except RuntimeError:
        raise HTTPException(status_code=400, detail="Invalid request") from None


@app.get("/sessions/{session_id}/audit")
async def get_session_audit(
    session_id: str,
    limit: int = 200,
    event_type: str | None = None,
) -> dict:
    """Return audit events for a session as JSON, newest first."""
    events = await manager.audit.list(
        session_id=session_id,
        limit=min(limit, 5000),
        event_type=event_type or None,
    )
    return {
        "session_id": session_id,
        "count": len(events),
        "events": [e.model_dump() for e in events],
    }


@app.get("/sessions/{session_id}/witness")
async def get_session_witness(session_id: str, limit: int = 100) -> dict:
    receipts = await manager.list_witness_receipts(session_id, limit=min(limit, 5000))
    return {
        "session_id": session_id,
        "count": len(receipts),
        "receipts": receipts,
    }


@app.get("/sessions/{session_id}/export-script")
async def export_script(session_id: str) -> dict:
    """Export session actions as a runnable Playwright Python script."""
    from .playwright_export import export_session_script
    session = await manager.get_session(session_id)
    return await export_session_script(
        session_id,
        manager.audit,
        start_url=session.page.url,
        viewport_w=settings.default_viewport_width,
        viewport_h=settings.default_viewport_height,
    )


@app.get("/sessions/{session_id}/trace")
async def get_trace(session_id: str) -> dict:
    """Return trace file metadata and download URL."""
    from pathlib import Path as _Path
    session = await manager.get_session(session_id)
    trace_path = _Path(str(session.trace_path)) if hasattr(session, "trace_path") else None
    if trace_path and trace_path.exists():
        return {
            "session_id": session_id,
            "trace_path": str(trace_path),
            "trace_url": f"/artifacts/{session_id}/{trace_path.name}",
            "trace_size_bytes": trace_path.stat().st_size,
            "viewer_url": f"https://trace.playwright.dev/?trace=/artifacts/{session_id}/{trace_path.name}",
        }
    return {"session_id": session_id, "trace_path": None, "trace_url": None, "viewer_url": None}
