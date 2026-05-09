from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

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
from .routes.agent import create_agent_router
from .routes.auth_profiles import create_auth_profiles_router
from .routes.mcp import create_mcp_router
from .routes.operations import create_operations_router
from .routes.session_diagnostics import create_session_diagnostics_router
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
app.include_router(create_session_diagnostics_router(manager=manager, settings=settings))
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


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str) -> dict:
    return await manager.close_session(session_id)


# ── Extended browser endpoints ──────────────────────────────────────────────

@app.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, name: str | None = None, start_url: str | None = None) -> dict:
    try:
        return await manager.fork_session(session_id, name=name, start_url=start_url)
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Conflict") from None
