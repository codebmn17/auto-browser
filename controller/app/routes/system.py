from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from ..audit import get_current_operator
from ..readiness import run_readiness_checks

if TYPE_CHECKING:
    from ..browser_manager import BrowserManager
    from ..config import Settings
    from ..maintenance import MaintenanceService
    from ..metrics import MetricsRecorder
    from ..orchestrator import BrowserOrchestrator


def create_system_router(
    *,
    settings: "Settings",
    manager: "BrowserManager",
    metrics: "MetricsRecorder",
    maintenance: "MaintenanceService",
    orchestrator: "BrowserOrchestrator",
    version: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> dict[str, str]:
        try:
            await manager.ensure_browser()
            return {"status": "ready", "environment": settings.environment_name}
        except Exception:
            raise HTTPException(status_code=503, detail="Service unavailable") from None

    @router.get("/version")
    async def get_version() -> dict[str, str]:
        return {"version": version}

    @router.get("/metrics", include_in_schema=False)
    async def get_metrics() -> Response:
        if not metrics.enabled:
            raise HTTPException(status_code=404, detail="Metrics disabled")
        metrics.set_active_sessions(len(manager.sessions))
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    @router.get("/maintenance/status")
    async def get_maintenance_status() -> dict[str, Any]:
        return {
            "cleanup_on_startup": settings.cleanup_on_startup,
            "cleanup_interval_seconds": settings.cleanup_interval_seconds,
            "artifact_retention_hours": settings.artifact_retention_hours,
            "upload_retention_hours": settings.upload_retention_hours,
            "auth_retention_hours": settings.auth_retention_hours,
            "last_report": maintenance.last_report,
        }

    @router.post("/maintenance/cleanup")
    async def run_maintenance_cleanup() -> dict[str, Any]:
        return await maintenance.run_cleanup()

    @router.get("/readiness")
    async def get_readiness(mode: str = "normal") -> JSONResponse:
        if mode not in {"normal", "confidential"}:
            raise HTTPException(status_code=400, detail="mode must be 'normal' or 'confidential'")
        report = run_readiness_checks(settings, mode=mode)
        return JSONResponse(
            content=report.to_dict(),
            status_code=200 if report.overall != "fail" else 503,
        )

    @router.get("/agent/providers")
    async def list_agent_providers() -> list[dict[str, Any]]:
        return [item.model_dump() for item in orchestrator.list_providers()]

    @router.get("/operator")
    async def get_operator() -> dict[str, Any]:
        return get_current_operator().model_dump()

    return router
