from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
import random
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

try:  # pragma: no cover - optional until dependency is installed in runtime image
    import pyotp
except Exception:  # pragma: no cover - graceful fallback for non-login test runs
    pyotp = None  # type: ignore[assignment]

from . import events as _events
from .action_errors import BrowserActionError
from .actions import ActionRunContext, BrowserActionPipeline
from .approvals import ApprovalRequiredError, ApprovalStore
from .artifacts import SessionArtifactService
from .audit import AuditStore, get_current_operator
from .auth_state import AuthStateManager
from .browser.services import (
    BrowserAuthProfileService,
    BrowserDiagnosticsService,
    BrowserTabService,
    BrowserUploadService,
)
from .browser_scripts import (
    ACTIVE_ELEMENT_SCRIPT,
    INTERACTABLES_SCRIPT,
    PAGE_SUMMARY_SCRIPT,
    apply_stealth,
)
from .config import Settings
from .downloads import DownloadCaptureService
from .memory_manager import MemoryManager
from .models import (
    ApprovalKind,
    BrowserActionDecision,
    SessionRecord,
    SessionStatus,
    WitnessRemoteState,
)
from .network_inspector import NetworkInspector
from .ocr import OCRExtractor
from .pii_scrub import PiiScrubber
from .session_isolation import DockerBrowserNodeProvisioner, IsolatedBrowserRuntime
from .session_store import DurableSessionStore
from .session_tunnel import IsolatedSessionTunnel, IsolatedSessionTunnelBroker
from .utils import UTC, utc_now
from .webhooks import dispatch_approval_event
from .witness import (
    WitnessActionContext,
    WitnessApproval,
    WitnessEvidence,
    WitnessPolicyEngine,
    WitnessPolicyOutcome,
    WitnessRecorder,
    WitnessRemoteClient,
    WitnessSessionContext,
)

logger = logging.getLogger(__name__)

ACCESSIBILITY_NODE_LIMIT = 30

__all__ = ["BrowserManager", "BrowserSession", "PlaywrightError"]


@dataclass
class BrowserSession:
    id: str
    name: str
    created_at: datetime
    context: BrowserContext
    page: Page
    artifact_dir: Path
    auth_dir: Path
    upload_dir: Path
    takeover_url: str
    trace_path: Path
    trace_recording: bool = False
    browser_node_name: str = "browser-node"
    isolation_mode: str = "shared_browser_node"
    browser: Browser | None = None
    runtime: IsolatedBrowserRuntime | None = None
    tunnel: IsolatedSessionTunnel | None = None
    shared_takeover_surface: bool = True
    shared_browser_process: bool = True
    max_live_sessions_per_browser_node: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    attached_pages: set[int] = field(default_factory=set)
    last_action: str | None = None
    proxy_persona: str | None = None
    last_auth_state_path: Path | None = None
    auth_profile_name: str | None = None
    tunnel_error: str | None = None
    mouse_position: tuple[float, float] | None = None
    totp_secret: str | None = None
    network_inspector: NetworkInspector | None = None
    # Headless/headed state — set to False to request headed mode on next fork
    headless: bool = True
    protection_mode: str = "normal"
    pending_witness_context: dict[str, Any] | None = None
    witness_remote_state: WitnessRemoteState = field(default_factory=WitnessRemoteState)
    metadata: dict[str, Any] = field(default_factory=dict)


SessionCreatedHook = Callable[[str, Page], Awaitable[None]]
SessionClosedHook = Callable[[str], Awaitable[None]]


class BrowserManager:
    def __init__(self, settings: Settings, *, proxy_store: Any | None = None):
        self.settings = settings
        self.proxy_store = proxy_store
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self._browser_lock = asyncio.Lock()
        self.action_pipeline = BrowserActionPipeline()
        self.auth_profiles = BrowserAuthProfileService(self)
        self.tabs = BrowserTabService(self)
        self.uploads = BrowserUploadService(self)

        Path(self.settings.artifact_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.upload_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.auth_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.approval_root).mkdir(parents=True, exist_ok=True)
        Path(self.settings.audit_root).mkdir(parents=True, exist_ok=True)
        witness_root = Path(self.settings.witness_root)
        try:
            witness_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            witness_root = Path(self.settings.audit_root).resolve().parent / "witness"
            witness_root.mkdir(parents=True, exist_ok=True)
            self.settings.witness_root = str(witness_root)
        if self.settings.state_db_path:
            Path(self.settings.state_db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        Path(self.settings.session_store_root).mkdir(parents=True, exist_ok=True)
        approval_kwargs: dict[str, Any] = {"db_path": self.settings.state_db_path}
        if "approval_ttl_minutes" in inspect.signature(ApprovalStore).parameters:
            approval_kwargs["approval_ttl_minutes"] = self.settings.approval_ttl_minutes
        self.approvals = ApprovalStore(self.settings.approval_root, **approval_kwargs)
        self.audit = AuditStore(
            self.settings.audit_root,
            db_path=self.settings.state_db_path,
            max_events=self.settings.audit_max_events,
        )
        self.artifacts = SessionArtifactService(self.settings.artifact_root)
        self.download_capture = DownloadCaptureService(self.artifacts)
        self.session_store = DurableSessionStore(
            file_root=self.settings.session_store_root,
            redis_url=self.settings.redis_url,
            redis_prefix=self.settings.session_store_redis_prefix,
        )
        self.memory = MemoryManager(settings.memory_root) if settings.memory_enabled else None
        self.auth_state = AuthStateManager(
            encryption_key=self.settings.auth_state_encryption_key,
            require_encryption=self.settings.require_auth_state_encryption,
            max_age_hours=self.settings.auth_state_max_age_hours,
        )
        self.ocr = OCRExtractor(
            enabled=self.settings.ocr_enabled,
            language=self.settings.ocr_language,
            max_blocks=self.settings.ocr_max_blocks,
            text_limit=self.settings.ocr_text_limit,
        )
        self.pii_scrubber = PiiScrubber.from_settings(self.settings)
        self.diagnostics = BrowserDiagnosticsService(
            self,
            self.pii_scrubber,
            self.download_capture,
        )
        self.witness = WitnessRecorder(self.settings.witness_root)
        self.witness_remote = WitnessRemoteClient(
            base_url=self.settings.witness_remote_url,
            api_key=self.settings.witness_remote_api_key,
            tenant_id=self.settings.witness_remote_tenant_id,
            timeout_seconds=self.settings.witness_remote_timeout_seconds,
            verify_tls=self.settings.witness_remote_verify_tls,
        )
        self.witness_policy = WitnessPolicyEngine()
        self.runtime_provisioner = DockerBrowserNodeProvisioner(self.settings)
        self.tunnel_broker = IsolatedSessionTunnelBroker(self.settings)
        self._session_created_hook: SessionCreatedHook | None = None
        self._session_closed_hook: SessionClosedHook | None = None

    def register_extension_hooks(
        self,
        *,
        session_created: SessionCreatedHook | None = None,
        session_closed: SessionClosedHook | None = None,
    ) -> None:
        self._session_created_hook = session_created
        self._session_closed_hook = session_closed

    def get_remote_access_info(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            session = self.sessions.get(session_id)
            if session is not None:
                return self._session_remote_access_info(session)
        return self._global_remote_access_info()

    def _global_remote_access_info(self) -> dict[str, Any]:
        info_path = Path(self.settings.remote_access_info_path)
        payload: dict[str, Any] = {
            "active": False,
            "status": "inactive",
            "stale": False,
            "source": "static",
            "configured_takeover_url": self.settings.takeover_url,
            "takeover_url": self.settings.takeover_url,
            "api_url": None,
            "api_auth_enabled": bool(self.settings.api_bearer_token),
            "info_path": str(info_path),
            "exists": info_path.exists(),
            "last_updated": None,
            "age_seconds": None,
            "stale_after_seconds": float(self.settings.remote_access_stale_after_seconds),
            "tunnel": None,
            "error": None,
        }
        if not info_path.exists():
            return payload
        try:
            tunnel = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to read remote access info %s: %s", info_path, exc)
            payload["status"] = "error"
            payload["source"] = "metadata_file"
            payload["error"] = "remote_access_metadata_unreadable"
            return payload

        last_updated = self._parse_remote_access_timestamp(tunnel.get("updated_at"))
        if last_updated is None:
            try:
                last_updated = datetime.fromtimestamp(info_path.stat().st_mtime, tz=UTC)
            except OSError:
                last_updated = None
        age_seconds = None
        if last_updated is not None:
            age_seconds = max(0.0, (datetime.now(UTC) - last_updated).total_seconds())
        stale_after_seconds = float(
            tunnel.get("stale_after_seconds") or self.settings.remote_access_stale_after_seconds
        )
        raw_status = str(tunnel.get("status") or "active")
        stale = bool(age_seconds is not None and age_seconds > stale_after_seconds)
        active = raw_status == "active" and not stale
        takeover_url = tunnel.get("public_takeover_url") if active else self.settings.takeover_url
        api_url = tunnel.get("public_api_url") if active else None
        payload.update(
            {
                "active": active,
                "status": "stale" if stale else raw_status,
                "stale": stale,
                "source": "metadata_file",
                "takeover_url": takeover_url,
                "api_url": api_url,
                "last_updated": (
                    last_updated.isoformat().replace("+00:00", "Z")
                    if last_updated is not None
                    else None
                ),
                "age_seconds": age_seconds,
                "stale_after_seconds": stale_after_seconds,
                "tunnel": tunnel,
            }
        )
        return payload

    def _session_remote_access_info(self, session: BrowserSession) -> dict[str, Any]:
        if session.isolation_mode != "docker_ephemeral":
            return self._global_remote_access_info()

        shared_remote_access = self._global_remote_access_info()
        takeover_url = session.takeover_url
        takeover_local_only = self._takeover_url_is_local_only(takeover_url)
        api_url = shared_remote_access.get("api_url")
        session_tunnel = self.tunnel_broker.describe(session.tunnel)
        warning = None
        status = "active"
        active = True
        effective_takeover_url = takeover_url
        requires_direct_host_access = takeover_local_only
        local_only = takeover_local_only

        if session_tunnel and session_tunnel.get("active"):
            effective_takeover_url = str(session_tunnel["public_takeover_url"])
            requires_direct_host_access = False
            local_only = False
            status = "active"
            active = True
        elif not takeover_local_only:
            status = "active"
            active = True
            requires_direct_host_access = False
            local_only = False
        else:
            active = False
            status = "api_only" if api_url else "local_only"
            warning = (
                "This isolated takeover URL is still bound to a local host/port. "
                "Enable ISOLATED_TUNNEL_* settings or set ISOLATED_TAKEOVER_HOST to a remotely reachable hostname "
                "or IP if humans need remote takeover."
            )
            if session_tunnel and session_tunnel.get("status") in {"error", "degraded"}:
                status = "degraded"
                warning = (
                    "The isolated session tunnel is unavailable, so takeover fell back to the local-only URL. "
                    f"{session_tunnel.get('error') or ''}"
                ).strip()
            elif session.tunnel_error:
                status = "degraded"
                warning = (
                    "The isolated session tunnel could not be created, so takeover fell back to the local-only URL. "
                    f"{session.tunnel_error}"
                ).strip()

        payload = dict(shared_remote_access)
        payload.update(
            {
                "session_id": session.id,
                "source": (
                    "isolated_session_tunnel"
                    if session_tunnel and session_tunnel.get("active")
                    else "isolated_runtime"
                ),
                "configured_takeover_url": takeover_url,
                "takeover_url": effective_takeover_url,
                "local_only": local_only,
                "requires_direct_host_access": requires_direct_host_access,
                "shared_api_url": api_url,
                "shared_tunnel_active": bool(shared_remote_access.get("active")),
                "shared_tunnel": shared_remote_access.get("tunnel"),
                "session_tunnel": session_tunnel,
                "session_tunnel_error": session.tunnel_error,
                "active": active,
                "status": status,
                "warning": warning,
            }
        )
        if session.runtime is not None:
            payload["runtime"] = {
                "container_name": session.runtime.container_name,
                "browser_node": session.runtime.browser_node_name,
                "novnc_port": session.runtime.novnc_port,
                "vnc_port": session.runtime.vnc_port,
            }
        return payload

    def _current_takeover_url(self, session: BrowserSession | None = None) -> str:
        if session is not None and session.isolation_mode == "docker_ephemeral":
            tunnel = self.tunnel_broker.describe(session.tunnel)
            if tunnel and tunnel.get("active") and tunnel.get("public_takeover_url"):
                return str(tunnel["public_takeover_url"])
            return session.takeover_url
        remote_access = self._global_remote_access_info()
        if remote_access.get("active") and remote_access.get("takeover_url"):
            return str(remote_access["takeover_url"])
        if session is not None:
            return session.takeover_url
        return self.settings.takeover_url

    @staticmethod
    def _parse_remote_access_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _takeover_url_is_local_only(value: str) -> bool:
        host = (urlparse(value).hostname or "").strip().lower()
        return host in {"", "127.0.0.1", "localhost", "::1", "0.0.0.0"}

    async def startup(self) -> None:
        logger.info("starting browser manager")
        await self.approvals.startup()
        await self.audit.startup()
        await self.witness.startup()
        if self.settings.witness_enabled:
            await self.witness_remote.startup()
        await self.session_store.startup()
        await self.session_store.mark_all_active_interrupted()
        if self.memory is not None:
            await self.memory.startup()
        self.playwright = await async_playwright().start()
        await self.tunnel_broker.startup()
        await self.runtime_provisioner.startup()
        if self.settings.session_isolation_mode == "shared_browser_node":
            await self.ensure_browser()

    async def shutdown(self) -> None:
        logger.info("shutting down browser manager")
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            try:
                await self.close_session(session_id)
            except Exception as exc:  # pragma: no cover - best effort cleanup
                logger.warning("failed to close session %s during shutdown: %s", session_id, exc)

        self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
        await self.tunnel_broker.shutdown()
        await self.witness_remote.shutdown()
        await self.session_store.shutdown()

    async def ensure_browser(self) -> Browser:
        async with self._browser_lock:
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            if self.playwright is None:
                raise RuntimeError("Playwright not started")

            # CDP attach mode: connect to an already-running Chrome instance
            if self.settings.cdp_connect_url:
                logger.info("connecting to existing Chrome via CDP at %s", self.settings.cdp_connect_url)
                self.browser = await self.playwright.chromium.connect_over_cdp(
                    self.settings.cdp_connect_url
                )
                logger.info("CDP attach succeeded")
                return self.browser

            self.browser = await self._connect_browser(
                self._resolve_browser_ws_endpoint,
                failure_context=(
                    "Unable to connect to browser node via Playwright server. "
                    f"Checked ws endpoint file {self.settings.browser_ws_endpoint_file} "
                    f"and direct endpoint {self.settings.browser_ws_endpoint or '<not configured>'}."
                ),
            )
            return self.browser

    async def cdp_attach(self, cdp_url: str) -> dict[str, Any]:
        """Attach to an existing Chrome/Chromium instance via CDP URL.

        This replaces the current shared browser connection. Sessions created
        after calling this will use pages from the attached browser.
        """
        if self.playwright is None:
            raise RuntimeError("Playwright not started")
        async with self._browser_lock:
            browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
            self.browser = browser
            logger.info("attached to Chrome via CDP at %s", cdp_url)
            await self.audit.append(
                event_type="cdp_attach",
                status="ok",
                action="cdp_attach",
                session_id=None,
                details={"cdp_url": cdp_url},
            )
            return {
                "attached": True,
                "cdp_url": cdp_url,
                "browser_version": browser.version,
            }

    async def _connect_browser(self, ws_target_factory, *, failure_context: str) -> Browser:
        if self.playwright is None:
            raise RuntimeError("Playwright not started")

        last_error: Exception | None = None
        for attempt in range(1, self.settings.connect_retries + 1):
            try:
                ws_target = await ws_target_factory()
                browser = await self.playwright.chromium.connect(ws_target)
                logger.info(
                    "connected to browser node on attempt %s via playwright endpoint %s",
                    attempt,
                    ws_target,
                )
                return browser
            except Exception as exc:  # pragma: no cover - depends on external service
                last_error = exc
                await asyncio.sleep(self.settings.connect_retry_delay_seconds)
        raise RuntimeError(failure_context) from last_error

    async def _resolve_browser_ws_endpoint(self) -> str:
        ws_endpoint_file = Path(self.settings.browser_ws_endpoint_file)
        if ws_endpoint_file.exists():
            ws_endpoint = ws_endpoint_file.read_text(encoding="utf-8").strip()
            if ws_endpoint:
                return ws_endpoint
        if self.settings.browser_ws_endpoint:
            return self.settings.browser_ws_endpoint
        raise FileNotFoundError(f"missing playwright ws endpoint file: {ws_endpoint_file}")

    async def _acquire_session_browser(self, session_id: str) -> tuple[Browser, IsolatedBrowserRuntime | None]:
        if self.settings.session_isolation_mode != "docker_ephemeral":
            return await self.ensure_browser(), None

        runtime = await self.runtime_provisioner.provision(session_id)
        try:
            browser = await self._connect_browser(
                lambda: asyncio.sleep(0, result=runtime.ws_endpoint),
                failure_context=(
                    "Unable to connect to isolated browser node via Playwright server. "
                    f"Checked isolated endpoint file {runtime.ws_endpoint_file}."
                ),
            )
            return browser, runtime
        except Exception:
            await self.runtime_provisioner.release(runtime)
            raise

    async def list_sessions(self) -> list[dict[str, Any]]:
        session_map = {
            record.id: record.model_dump()
            for record in await self.session_store.list()
        }
        for session in self.sessions.values():
            summary = await self._session_summary(session)
            session_map[summary["id"]] = summary
        return sorted(
            session_map.values(),
            key=lambda item: (item.get("created_at") or "", item.get("id") or ""),
            reverse=True,
        )

    async def create_session(
        self,
        *,
        name: str | None = None,
        start_url: str | None = None,
        storage_state_path: str | None = None,
        auth_profile: str | None = None,
        memory_profile: str | None = None,
        proxy_persona: str | None = None,
        request_proxy_server: str | None = None,
        request_proxy_username: str | None = None,
        request_proxy_password: str | None = None,
        user_agent: str | None = None,
        protection_mode: str | None = None,
        totp_secret: str | None = None,
    ) -> dict[str, Any]:
        if storage_state_path and auth_profile:
            raise ValueError("Provide auth_profile or storage_state_path, not both")
        if proxy_persona and any((request_proxy_server, request_proxy_username, request_proxy_password)):
            raise ValueError("Provide proxy_persona or explicit proxy_server credentials, not both")
        if start_url:
            self._assert_url_allowed(start_url)
        resolved_protection_mode = protection_mode or self.settings.witness_protection_mode_default
        self._check_session_limit()

        session_id = uuid4().hex[:12]
        artifact_dir, auth_dir, upload_dir = self._prepare_session_dirs(session_id)
        prepared_auth_state = None
        source_path: Path | None = None

        if proxy_persona:
            if self.proxy_store is None:
                raise RuntimeError("No PROXY_PERSONA_FILE configured")
            resolved_proxy = self.proxy_store.resolve_proxy(proxy_persona)
            proxy_server = resolved_proxy.get("server")
            proxy_username = resolved_proxy.get("username")
            proxy_password = resolved_proxy.get("password")
        else:
            proxy_server = request_proxy_server or self.settings.default_proxy_server
            proxy_username = request_proxy_username or self.settings.default_proxy_username
            proxy_password = request_proxy_password or self.settings.default_proxy_password

        context_kwargs = self._build_context_kwargs(user_agent, proxy_server, proxy_username, proxy_password)

        if auth_profile:
            source_path = self._resolve_auth_profile_state_path(auth_profile, must_exist=True)
        elif storage_state_path:
            source_path = self._safe_auth_path(storage_state_path, must_exist=True)
        if source_path is not None:
            prepared_auth_state = self.auth_state.prepare_for_context(source_path)
            context_kwargs["storage_state"] = str(prepared_auth_state.path)

        context: BrowserContext | None = None
        session: BrowserSession | None = None
        browser: Browser | None = None
        runtime: IsolatedBrowserRuntime | None = None
        try:
            browser, runtime = await self._acquire_session_browser(session_id)
            context = await browser.new_context(**context_kwargs)
            if self.settings.enable_tracing:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)

            page = await context.new_page()
            page.set_default_timeout(self.settings.action_timeout_ms)
            if self.settings.stealth_enabled:
                await apply_stealth(page)
            session = BrowserSession(
                id=session_id,
                name=name or f"session-{session_id}",
                created_at=datetime.now(UTC),
                context=context,
                page=page,
                artifact_dir=artifact_dir,
                auth_dir=auth_dir,
                upload_dir=upload_dir,
                takeover_url=runtime.takeover_url if runtime is not None else self.settings.takeover_url,
                trace_path=artifact_dir / "trace.zip",
                trace_recording=self.settings.enable_tracing,
                browser_node_name=runtime.browser_node_name if runtime is not None else "browser-node",
                isolation_mode=self.settings.session_isolation_mode,
                browser=browser,
                runtime=runtime,
                shared_takeover_surface=runtime is None,
                shared_browser_process=runtime is None,
                max_live_sessions_per_browser_node=1,
                proxy_persona=proxy_persona,
                last_auth_state_path=source_path if storage_state_path else None,
                auth_profile_name=self._normalize_auth_profile_name(auth_profile) if auth_profile else None,
                mouse_position=(
                    self.settings.default_viewport_width / 2,
                    self.settings.default_viewport_height / 2,
                ),
                protection_mode=resolved_protection_mode,
                totp_secret=totp_secret,
                witness_remote_state=self._initial_witness_remote_state(resolved_protection_mode),
            )
            if source_path is not None:
                session.last_auth_state_path = source_path
            self._attach_page_listeners(page, session)
            if hasattr(context, "on"):
                context.on("page", lambda popup: self._attach_page_listeners(popup, session))

            if self.settings.network_inspector_enabled:
                inspector = NetworkInspector(
                    session_id=session_id,
                    max_entries=self.settings.network_inspector_max_entries,
                    capture_bodies=self.settings.network_inspector_capture_bodies,
                    body_max_bytes=self.settings.network_inspector_body_max_bytes,
                    scrubber=self.pii_scrubber if self.settings.pii_scrub_enabled else None,
                )
                inspector.attach(page)
                session.network_inspector = inspector

            self.sessions[session_id] = session
            if self._session_created_hook is not None:
                try:
                    await self._session_created_hook(session_id, page)
                except Exception as exc:
                    logger.warning("session created hook failed for %s: %s", session_id, exc)

            if start_url:
                await page.goto(start_url, wait_until="domcontentloaded")
                await self._settle(page)

            await self._maybe_provision_session_tunnel(session)
            if memory_profile and self.memory is not None:
                memory = await self.memory.get(memory_profile)
                if memory is not None:
                    session.metadata["memory_context"] = memory.to_system_prompt()
                    session.metadata["memory_profile"] = memory_profile
                    logger.info("memory profile loaded: %s", memory_profile)
            await self._persist_session(session, status="active")
            await self._record_session_witness_receipt(
                session,
                action="create_session",
                status="ok",
                metadata={
                    "start_url": start_url,
                    "storage_state_path": storage_state_path,
                    "auth_profile": auth_profile,
                    "memory_profile": memory_profile,
                    "proxy_persona": proxy_persona,
                    "totp_enabled": bool(totp_secret),
                },
            )
            await self._persist_session(session, status="active")
            summary = await self._session_summary(session)
            await self.audit.append(
                event_type="session_created",
                status="ok",
                action="create_session",
                session_id=session.id,
                details={
                    "start_url": start_url,
                    "storage_state_path": storage_state_path,
                    "auth_profile": auth_profile,
                    "memory_profile": memory_profile,
                    "proxy_persona": proxy_persona,
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                    "totp_enabled": bool(totp_secret),
                },
            )
            return summary
        except Exception:
            await self._cleanup_failed_session(session_id, session=session, context=context,
                                               browser=browser, runtime=runtime)
            raise
        finally:
            if prepared_auth_state is not None:
                prepared_auth_state.cleanup()

    def _check_session_limit(self) -> None:
        if len(self.sessions) >= self.settings.max_sessions:
            active_ids = ", ".join(sorted(self.sessions.keys()))
            message = (
                f"Session limit reached: max_sessions={self.settings.max_sessions}. "
                f"Active live session(s): {active_ids}."
            )
            if self.settings.session_isolation_mode == "shared_browser_node":
                message += (
                    " This scaffold uses one visible desktop and one shared browser node by default, "
                    "so only one live workflow is allowed unless you switch to docker_ephemeral isolation."
                )
            raise RuntimeError(message)

    def _prepare_session_dirs(self, session_id: str) -> tuple[Path, Path, Path]:
        artifact_dir = self.artifacts.prepare_session_dir(session_id)
        auth_dir = self._session_auth_root(session_id)
        upload_dir = self._session_upload_root(session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir, auth_dir, upload_dir

    def _build_context_kwargs(
        self,
        user_agent: str | None,
        proxy_server: str | None,
        proxy_username: str | None,
        proxy_password: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.default_viewport_width,
                "height": self.settings.default_viewport_height,
            },
            "accept_downloads": True,
        }
        effective_ua = user_agent or (self.settings.random_user_agent if self.settings.stealth_enabled else None)
        if effective_ua:
            kwargs["user_agent"] = effective_ua
        if self.settings.stealth_enabled:
            kwargs.setdefault("timezone_id", "America/New_York")
            kwargs.setdefault("locale", "en-US")
            kwargs.setdefault("extra_http_headers", {"Accept-Language": "en-US,en;q=0.9"})
        if proxy_server:
            proxy_cfg: dict[str, Any] = {"server": proxy_server}
            if proxy_username:
                proxy_cfg["username"] = proxy_username
            if proxy_password:
                proxy_cfg["password"] = proxy_password
            kwargs["proxy"] = proxy_cfg
        return kwargs

    async def _cleanup_failed_session(
        self,
        session_id: str,
        *,
        session: "BrowserSession | None",
        context: "BrowserContext | None",
        browser: "Browser | None",
        runtime: "IsolatedBrowserRuntime | None",
    ) -> None:
        self.sessions.pop(session_id, None)
        if session is not None and session.tunnel is not None:
            try:
                await self.tunnel_broker.release(session.tunnel)
            except Exception as exc:
                logger.warning("failed to release session tunnel during create_session rollback: %s", exc)
        if context is not None:
            try:
                await context.close()
            except Exception as exc:
                logger.warning("failed to close browser context during create_session rollback: %s", exc)
        if browser is not None and browser is not self.browser:
            try:
                await browser.close()
            except Exception as exc:
                logger.warning("failed to close isolated browser during create_session rollback: %s", exc)
        if runtime is not None:
            try:
                await self.runtime_provisioner.release(runtime)
            except Exception as exc:
                logger.warning("failed to release isolated runtime during create_session rollback: %s", exc)

    async def get_session(self, session_id: str) -> BrowserSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def get_session_record(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is not None:
            return await self._session_summary(session)
        record = await self.session_store.get(session_id)
        return record.model_dump()

    async def list_approvals(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        approvals = await self.approvals.list(status=status, session_id=session_id)
        return [approval.model_dump() for approval in approvals]

    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        approval = await self.approvals.get(approval_id)
        return approval.model_dump()

    async def approve(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        approval = await self.approvals.approve(approval_id, comment=comment)
        session = self.sessions.get(approval.session_id)
        await self.audit.append(
            event_type="approval_decision",
            status="approved",
            action="approve",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "comment": comment},
        )
        if session is not None:
            await self._record_witness_receipt(
                session,
                event_type="approval",
                status="approved",
                action="approve",
                action_class="control",
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=approval.status,
                    reason=approval.reason,
                ),
                target={"kind": approval.kind, "action": approval.action.action},
                metadata={"comment": comment},
            )
        return approval.model_dump()

    async def reject(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        approval = await self.approvals.reject(approval_id, comment=comment)
        session = self.sessions.get(approval.session_id)
        await self.audit.append(
            event_type="approval_decision",
            status="rejected",
            action="reject",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "comment": comment},
        )
        if session is not None:
            await self._record_witness_receipt(
                session,
                event_type="approval",
                status="rejected",
                action="reject",
                action_class="control",
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=approval.status,
                    reason=approval.reason,
                ),
                target={"kind": approval.kind, "action": approval.action.action},
                metadata={"comment": comment},
            )
        return approval.model_dump()

    async def execute_approval(self, approval_id: str) -> dict[str, Any]:
        approval = await self.approvals.get(approval_id)
        if approval.status != "approved":
            raise PermissionError(f"approval {approval_id} is not approved")

        decision = approval.action
        if decision.action == "upload":
            execution = await self.upload(
                approval.session_id,
                selector=decision.selector,
                element_id=decision.element_id,
                file_path=decision.file_path or "",
                approved=False,
                approval_id=approval.id,
            )
            latest = await self.approvals.get(approval.id)
        else:
            execution = await self.execute_decision(
                approval.session_id,
                decision,
                approval_id=approval.id,
            )
            latest = await self.approvals.get(approval.id)
        await self.audit.append(
            event_type="approval_executed",
            status="ok",
            action="execute_approval",
            session_id=approval.session_id,
            approval_id=approval.id,
            details={"kind": approval.kind, "action": decision.action},
        )
        session = self.sessions.get(approval.session_id)
        if session is not None:
            await self._record_witness_receipt(
                session,
                event_type="approval",
                status="executed",
                action="execute_approval",
                action_class="control",
                approval=WitnessApproval(
                    required=True,
                    approval_id=approval.id,
                    status=latest.status,
                    reason=approval.reason,
                ),
                target={"kind": approval.kind, "action": decision.action},
            )
        return {
            "approval": latest.model_dump(),
            "execution": execution,
        }

    async def observe(self, session_id: str, limit: int = 40, preset: str = "normal") -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            result = await self._observation_payload(session, limit=limit, preset=preset)
            _events.emit_observe(
                session_id,
                result.get("url", ""),
                result.get("title", ""),
                result.get("screenshot_url"),
            )
            return result

    async def capture_screenshot(self, session_id: str, *, label: str = "manual") -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            screenshot = await self._capture_screenshot(session, label)
            return {
                "session": await self._session_summary(session),
                "url": session.page.url,
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
                "takeover_url": self._current_takeover_url(session),
            }

    async def get_console_messages(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_console_messages(session_id, limit=limit)

    async def get_network_log(
        self,
        session_id: str,
        *,
        limit: int = 100,
        method: str | None = None,
        url_contains: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            inspector = session.network_inspector
            if inspector is None:
                return {
                    "session": await self._session_summary(session),
                    "enabled": False,
                    "entries": [],
                    "summary": {},
                }
            return {
                "session": await self._session_summary(session),
                "enabled": True,
                "entries": inspector.entries(limit=limit, method=method, url_contains=url_contains),
                "summary": inspector.summary(),
            }

    async def fork_session(
        self,
        session_id: str,
        *,
        name: str | None = None,
        start_url: str | None = None,
    ) -> dict[str, Any]:
        """Fork a session: clone cookies + localStorage state into a new session."""
        session = await self.get_session(session_id)
        async with session.lock:
            # Export cookies and storage state to a temp file
            fork_auth_path = session.auth_dir / f"fork_{uuid4().hex[:8]}.json"
            await session.context.storage_state(path=str(fork_auth_path))
            current_url = session.page.url

        # Create the new session using the forked state
        forked = await self.create_session(
            name=name or f"fork-of-{session.name}",
            start_url=start_url or current_url,
            storage_state_path=str(fork_auth_path),
        )
        forked["forked_from"] = session_id
        await self.audit.append(
            event_type="session_forked",
            status="ok",
            action="fork_session",
            session_id=session_id,
            details={"new_session_id": forked["id"], "start_url": start_url or current_url},
        )
        return forked

    def get_pii_scrubber_status(self) -> dict[str, Any]:
        """Return current PII scrubber configuration."""
        return self.pii_scrubber.summary()

    async def enable_shadow_browse(self, session_id: str) -> dict[str, Any]:
        """Switch a session to headed (visible) mode for debugging.

        Because Playwright cannot flip headless→headed mid-session, this:
        1. Exports state (cookies + storage) from the running session
        2. Launches a new LOCAL headed Chromium process
        3. Creates a new BrowserSession with that state and the same URL
        4. Returns the new session's info (the old session keeps running)

        The caller is expected to close the original session when done debugging.
        """
        if not self.settings.shadow_browse_enabled:
            raise RuntimeError("Shadow browsing is disabled (SHADOW_BROWSE_ENABLED=false)")
        if self.playwright is None:
            raise RuntimeError("Playwright not started")

        session = await self.get_session(session_id)
        async with session.lock:
            current_url = session.page.url
            shadow_auth_path = session.auth_dir / f"shadow_{uuid4().hex[:8]}.json"
            await session.context.storage_state(path=str(shadow_auth_path))

        # Launch a local headed browser process
        headed_browser = await self.playwright.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )

        shadow_session_id = uuid4().hex[:12]
        artifact_dir = Path(self.settings.artifact_root) / shadow_session_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        auth_dir = self._session_auth_root(shadow_session_id)
        upload_dir = self._session_upload_root(shadow_session_id)
        auth_dir.mkdir(parents=True, exist_ok=True)
        upload_dir.mkdir(parents=True, exist_ok=True)

        context_kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.default_viewport_width,
                "height": self.settings.default_viewport_height,
            },
            "accept_downloads": True,
            "storage_state": str(shadow_auth_path),
        }
        context = await headed_browser.new_context(**context_kwargs)
        page = await context.new_page()
        page.set_default_timeout(self.settings.action_timeout_ms)
        if self.settings.stealth_enabled:
            await apply_stealth(page)

        shadow_session = BrowserSession(
            id=shadow_session_id,
            name=f"shadow-{session.name}",
            created_at=datetime.now(UTC),
            context=context,
            page=page,
            artifact_dir=artifact_dir,
            auth_dir=auth_dir,
            upload_dir=upload_dir,
            takeover_url=self.settings.takeover_url,
            trace_path=artifact_dir / "trace.zip",
            browser=headed_browser,
            headless=False,
        )
        self._attach_page_listeners(page, shadow_session)
        self.sessions[shadow_session_id] = shadow_session

        await page.goto(current_url, wait_until="domcontentloaded")
        await self._settle(page)
        await self._persist_session(shadow_session, status="active")
        await self.audit.append(
            event_type="shadow_browse_started",
            status="ok",
            action="enable_shadow_browse",
            session_id=session_id,
            details={"shadow_session_id": shadow_session_id, "url": current_url},
        )
        return {
            "shadow_session_id": shadow_session_id,
            "original_session_id": session_id,
            "url": current_url,
            "headless": False,
            "note": "Headed Chrome launched. Close the original session when done debugging.",
        }

    async def get_page_errors(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_page_errors(session_id, limit=limit)

    async def get_request_failures(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self.diagnostics.get_request_failures(session_id, limit=limit)

    async def stop_trace(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            await self._stop_trace_recording(session)
            return {
                "session": await self._session_summary(session),
                **self._trace_payload(session),
            }

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        self._assert_url_allowed(url)
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.goto(url, wait_until="domcontentloaded")
            await self._settle(session.page)
            # Check for bot challenge pages after navigation
            challenge = await self._check_bot_challenge(session)
            if challenge:
                logger.warning("bot challenge detected after navigation: %s", challenge)
                try:
                    await self.request_human_takeover(session.id, reason=f"Bot challenge detected: {challenge['signal']}")
                except Exception:
                    pass

        return await self._run_action(session, "navigate", {"url": url}, operation)

    async def click(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self._click_human_like(session, float(x), float(y))
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self._locator_center(locator)
                if coords is None:
                    await locator.click()
                else:
                    target["x"], target["y"] = coords
                    await self._click_human_like(session, coords[0], coords[1])
            await self._settle(session.page)

        return await self._run_action(session, "click", target, operation)

    async def hover(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id, x=x, y=y)

        async def operation() -> None:
            if target["mode"] == "coordinates":
                await self._move_mouse_human_like(session, float(x), float(y))
            else:
                locator = session.page.locator(target["selector"]).first
                await locator.scroll_into_view_if_needed()
                coords = await self._locator_center(locator)
                if coords is None:
                    await locator.hover()
                else:
                    target["x"], target["y"] = coords
                    await self._move_mouse_human_like(session, coords[0], coords[1])
            await self._settle(session.page)

        return await self._run_action(session, "hover", target, operation)

    async def select_option(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.scroll_into_view_if_needed()
            if index is not None:
                await locator.select_option(index=index)
            elif value is not None:
                await locator.select_option(value=value)
            else:
                await locator.select_option(label=label)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "select_option",
            {**target, "value": value, "label": label, "index": index},
            operation,
        )

    async def type(
        self,
        session_id: str,
        *,
        text: str,
        selector: str | None = None,
        element_id: str | None = None,
        clear_first: bool = True,
        sensitive: bool = False,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        target = self._resolve_target(selector=selector, element_id=element_id)
        payload = self._text_target_payload(target, text, clear_first=clear_first, sensitive=sensitive, preview_chars=80)

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            if await self._locator_is_sensitive_input(locator):
                payload.pop("text_preview", None)
                payload["text_redacted"] = True
            await locator.scroll_into_view_if_needed()
            await self._focus_locator(session, locator)
            if clear_first:
                await session.page.keyboard.press("Control+a")
                await asyncio.sleep(0.03)
                await session.page.keyboard.press("Delete")
                await asyncio.sleep(0.05)
            await self._type_text_human_like(session.page, text)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "type",
            payload,
            operation,
        )

    @staticmethod
    def _text_target_payload(
        target: dict[str, Any],
        text: str,
        *,
        clear_first: bool,
        sensitive: bool,
        preview_chars: int,
    ) -> dict[str, Any]:
        payload = {**target, "clear_first": clear_first}
        if sensitive:
            payload["text_redacted"] = True
        else:
            payload["text_preview"] = text[:preview_chars]
        return payload

    async def _locator_is_sensitive_input(self, locator: Any) -> bool:
        try:
            attributes = {
                "type": await locator.get_attribute("type"),
                "name": await locator.get_attribute("name"),
                "id": await locator.get_attribute("id"),
                "autocomplete": await locator.get_attribute("autocomplete"),
                "placeholder": await locator.get_attribute("placeholder"),
                "aria_label": await locator.get_attribute("aria-label"),
            }
        except Exception:
            return False

        input_type = (attributes.get("type") or "").strip().lower()
        if input_type == "password":
            return True

        autocomplete = (attributes.get("autocomplete") or "").strip().lower()
        if autocomplete in {"current-password", "new-password", "one-time-code"}:
            return True

        haystack = " ".join(str(value or "") for value in attributes.values()).lower()
        return bool(re.search(r"password|passcode|otp|one[- ]time|verification|token|secret|2fa|mfa", haystack))

    async def press(self, session_id: str, key: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.keyboard.press(key)
            await self._settle(session.page)

        return await self._run_action(session, "press", {"key": key}, operation)

    async def scroll(self, session_id: str, delta_x: float, delta_y: float) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.mouse.wheel(delta_x, delta_y)
            await self._settle(session.page)

        return await self._run_action(
            session,
            "scroll",
            {"delta_x": delta_x, "delta_y": delta_y},
            operation,
        )

    async def wait(self, session_id: str, wait_ms: int) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await asyncio.sleep(max(0, wait_ms) / 1000)

        return await self._run_action(session, "wait", {"wait_ms": wait_ms}, operation)

    async def reload(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.reload(wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "reload", {}, operation)

    async def go_back(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.go_back(wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "go_back", {}, operation)

    async def go_forward(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)

        async def operation() -> None:
            await session.page.go_forward(wait_until="domcontentloaded")
            await self._settle(session.page)

        return await self._run_action(session, "go_forward", {}, operation)

    async def list_tabs(self, session_id: str) -> list[dict[str, Any]]:
        return await self.tabs.list(session_id)

    async def open_tab(self, session_id: str, url: str | None, activate: bool) -> dict[str, Any]:
        return await self.tabs.open(session_id, url, activate)

    async def activate_tab(self, session_id: str, index: int) -> dict[str, Any]:
        return await self.tabs.activate(session_id, index)

    async def close_tab(self, session_id: str, index: int) -> dict[str, Any]:
        return await self.tabs.close(session_id, index)

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        return await self.diagnostics.list_downloads(session_id)

    async def _locator_center(self, locator: Any) -> tuple[float, float] | None:
        try:
            box = await locator.bounding_box()
        except Exception:
            return None
        if not box:
            return None
        return (float(box["x"] + box["width"] / 2), float(box["y"] + box["height"] / 2))

    async def _move_mouse_human_like(self, session: BrowserSession, x: float, y: float) -> None:
        start = session.mouse_position
        if start is None:
            start = (
                self.settings.default_viewport_width / 2 + random.randint(-120, 120),
                self.settings.default_viewport_height / 2 + random.randint(-80, 80),
            )
            await session.page.mouse.move(start[0], start[1])
            session.mouse_position = start

        start_x, start_y = start
        control_1 = (
            start_x + (x - start_x) * random.uniform(0.2, 0.4) + random.randint(-80, 80),
            start_y + (y - start_y) * random.uniform(0.1, 0.5) + random.randint(-80, 80),
        )
        control_2 = (
            start_x + (x - start_x) * random.uniform(0.6, 0.85) + random.randint(-60, 60),
            start_y + (y - start_y) * random.uniform(0.5, 0.9) + random.randint(-60, 60),
        )
        steps = random.randint(18, 34)
        for step in range(1, steps + 1):
            t = step / steps
            inv = 1 - t
            px = (
                inv**3 * start_x
                + 3 * inv * inv * t * control_1[0]
                + 3 * inv * t * t * control_2[0]
                + t**3 * x
            )
            py = (
                inv**3 * start_y
                + 3 * inv * inv * t * control_1[1]
                + 3 * inv * t * t * control_2[1]
                + t**3 * y
            )
            await session.page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.004, 0.018))
        session.mouse_position = (x, y)

    async def _click_human_like(self, session: BrowserSession, x: float, y: float) -> None:
        jitter_x = x + random.uniform(-2.5, 2.5)
        jitter_y = y + random.uniform(-2.5, 2.5)
        await self._move_mouse_human_like(session, jitter_x, jitter_y)
        await asyncio.sleep(random.uniform(0.03, 0.12))
        await session.page.mouse.down()
        await asyncio.sleep(random.uniform(0.02, 0.08))
        await session.page.mouse.up()
        session.mouse_position = (jitter_x, jitter_y)

    async def _focus_locator(self, session: BrowserSession, locator: Any) -> None:
        coords = await self._locator_center(locator)
        if coords is None:
            await locator.click()
        else:
            await self._click_human_like(session, coords[0], coords[1])
        await asyncio.sleep(0.05 + random.random() * 0.1)

    async def _type_text_human_like(self, page: Page, text: str) -> None:
        for index, char in enumerate(text):
            await page.keyboard.type(char)
            delay_ms = random.randint(
                self.settings.human_typing_min_delay_ms,
                self.settings.human_typing_max_delay_ms,
            )
            if index > 0 and index % random.randint(6, 12) == 0:
                delay_ms += random.randint(180, 600)
            await asyncio.sleep(delay_ms / 1000)

    async def _first_visible_locator(self, page: Page, selectors: list[str]) -> tuple[Any, str] | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return locator, selector
            except Exception:
                continue
        return None

    async def _maybe_handle_totp(self, session: BrowserSession) -> dict[str, Any] | None:
        if not session.totp_secret:
            return None
        if pyotp is None:
            raise BrowserActionError(
                "TOTP support is not installed in this controller runtime",
                action="totp_fill",
                code="totp_unavailable",
                retryable=False,
                details={"url": session.page.url},
            )
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"][maxlength="6"]',
            'input[name*="otp" i]',
            'input[name*="code" i]',
            'input[id*="otp" i]',
            'input[id*="code" i]',
            'input[aria-label*="code" i]',
            'input[placeholder*="code" i]',
        ]
        located = await self._first_visible_locator(session.page, selectors)
        if located is None:
            return None

        locator, selector = located
        code = pyotp.TOTP(session.totp_secret).now()
        await self._focus_locator(session, locator)
        try:
            await locator.fill("")
        except Exception:
            await session.page.keyboard.press("Control+a")
            await session.page.keyboard.press("Delete")
        await self._type_text_human_like(session.page, code)
        submit = await self._first_visible_locator(
            session.page,
            [
                'button[type="submit"]',
                '[aria-label*="verify" i][role="button"]',
                'button:has-text("Verify")',
                'button:has-text("Continue")',
                'button:has-text("Next")',
                'button:has-text("Submit")',
            ],
        )
        if submit is not None:
            coords = await self._locator_center(submit[0])
            if coords is None:
                await submit[0].click()
            else:
                await self._click_human_like(session, coords[0], coords[1])
        await self._settle(session.page)
        return {"selector": selector, "code_length": len(code)}

    @staticmethod
    def _host_matches(host: str, *domains: str) -> bool:
        host = host.lower().rstrip(".")
        for domain in domains:
            domain = domain.lower().rstrip(".")
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def _current_platform(self, session: BrowserSession) -> str | None:
        host = (urlparse(session.page.url).hostname or "").lower()
        if self._host_matches(host, "x.com", "twitter.com"):
            return "x"
        if self._host_matches(host, "instagram.com"):
            return "instagram"
        if self._host_matches(host, "linkedin.com"):
            return "linkedin"
        if self._host_matches(host, "outlook.live.com", "outlook.office.com", "outlook.office365.com"):
            return "outlook"
        return None

    async def execute_decision(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        approval = await self._require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
        )
        session.pending_witness_context = {
            "risk_category": decision.risk_category,
            "approval_id": approval_id or (approval.id if approval is not None else None),
            "approval_status": "approved" if approval_id or approval is not None else None,
            "runtime_requires_approval": approval is not None or approval_id is not None,
            "sensitive_input": bool(getattr(decision, "sensitive", False)),
        }
        try:
            if decision.action == "navigate":
                result = await self.navigate(session_id, decision.url or "")
            elif decision.action == "click":
                result = await self.click(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    x=decision.x,
                    y=decision.y,
                )
            elif decision.action == "hover":
                result = await self.hover(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    x=decision.x,
                    y=decision.y,
                )
            elif decision.action == "select_option":
                result = await self.select_option(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    value=decision.value,
                    label=decision.label,
                    index=decision.index,
                )
            elif decision.action == "type":
                result = await self.type(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    text=decision.text or "",
                    clear_first=decision.clear_first,
                    sensitive=decision.sensitive,
                )
            elif decision.action == "press":
                result = await self.press(session_id, decision.key or "")
            elif decision.action == "scroll":
                result = await self.scroll(session_id, decision.delta_x, decision.delta_y)
            elif decision.action == "wait":
                result = await self.wait(session_id, decision.wait_ms)
            elif decision.action == "reload":
                result = await self.reload(session_id)
            elif decision.action == "go_back":
                result = await self.go_back(session_id)
            elif decision.action == "go_forward":
                result = await self.go_forward(session_id)
            elif decision.action == "upload":
                result = await self.upload(
                    session_id,
                    selector=decision.selector,
                    element_id=decision.element_id,
                    file_path=decision.file_path or "",
                    approved=False,
                    approval_id=approval_id,
                )
                return result
            else:  # pragma: no cover - guarded by schema
                raise ValueError(f"Unsupported action: {decision.action}")

            if approval is not None:
                await self.approvals.mark_executed(approval.id)
            return result
        finally:
            session.pending_witness_context = None

    async def upload(
        self,
        session_id: str,
        *,
        file_path: str,
        approved: bool,
        approval_id: str | None = None,
        selector: str | None = None,
        element_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.uploads.upload(
            session_id,
            file_path=file_path,
            approved=approved,
            approval_id=approval_id,
            selector=selector,
            element_id=element_id,
        )

    async def save_storage_state(self, session_id: str, path: str) -> dict[str, Any]:
        return await self.auth_profiles.save_storage_state(session_id, path)

    async def _save_auth_profile_for_session(
        self,
        session: BrowserSession,
        profile_name: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.auth_profiles.save_for_session(session, profile_name, metadata=metadata)

    async def save_auth_profile(self, session_id: str, profile_name: str) -> dict[str, Any]:
        return await self.auth_profiles.save(session_id, profile_name)

    async def get_auth_profile(self, profile_name: str) -> dict[str, Any]:
        return await self.auth_profiles.get(profile_name)

    async def list_auth_profiles(self) -> list[dict[str, Any]]:
        return await self.auth_profiles.list()

    async def request_human_takeover(self, session_id: str, reason: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        payload = {
            "session": await self._session_summary(session),
            "reason": reason,
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "message": (
                "Human takeover requested. Open the noVNC URL to continue visually."
                if session.isolation_mode == "docker_ephemeral"
                else "Human takeover requested. Open the noVNC URL to continue visually. In this POC, takeover is global to the single browser desktop."
            ),
        }
        await self._append_jsonl(
            session.artifact_dir / "actions.jsonl",
            {"timestamp": utc_now(), "action": "request_human_takeover", **payload},
        )
        await self.audit.append(
            event_type="takeover_requested",
            status="ok",
            action="request_human_takeover",
            session_id=session.id,
            details={"reason": reason},
        )
        await self._record_witness_receipt(
            session,
            event_type="control",
            status="ok",
            action="request_human_takeover",
            action_class="control",
            target={"reason": reason},
        )
        payload["session"] = await self._session_summary(session)
        await self._persist_session(session, status="active")
        return payload

    async def _require_decision_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
        fallback_reason: str | None = None,
        approval_kind: ApprovalKind | None = None,
    ):
        kind = approval_kind or self._approval_kind_for_decision(decision)
        if kind is None:
            return None
        if approval_id:
            return await self.approvals.require_approved(
                approval_id=approval_id,
                session_id=session_id,
                kind=kind,
                action=decision,
            )

        session = await self.get_session(session_id)
        approval = await self.approvals.create_or_reuse_pending(
            session_id=session_id,
            kind=kind,
            reason=fallback_reason or decision.reason,
            action=decision,
            observation=await self._approval_observation(session),
        )
        await self._record_witness_receipt(
            session,
            event_type="approval",
            status="pending",
            action="approval_requested",
            action_class="control",
            risk_category=decision.risk_category,
            approval=WitnessApproval(
                required=True,
                approval_id=approval.id,
                status=approval.status,
                reason=approval.reason,
            ),
            target={
                "kind": approval.kind,
                "action": decision.action,
                "selector": decision.selector,
                "element_id": decision.element_id,
            },
            metadata={"reason": approval.reason},
        )
        # Emit SSE event
        _events.emit_approval(session_id, approval.id, approval.kind, approval.status, approval.reason)
        # Fire webhook if configured
        if self.settings.approval_webhook_url:
            asyncio.ensure_future(
                dispatch_approval_event(
                    approval,
                    webhook_url=self.settings.approval_webhook_url,
                    webhook_secret=self.settings.approval_webhook_secret,
                )
            )
        raise ApprovalRequiredError(approval)

    async def require_governed_approval(
        self,
        session_id: str,
        decision: BrowserActionDecision,
        *,
        approval_id: str | None,
    ):
        kind = self._governed_approval_kind_for_decision(decision)
        if kind is None:
            return None
        reason = (
            "Governed workflow requires operator approval before executing "
            f"{decision.risk_category or 'write'} action {decision.action!r}."
        )
        return await self._require_decision_approval(
            session_id,
            decision,
            approval_id=approval_id,
            fallback_reason=reason,
            approval_kind=kind,
        )

    async def close_session(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        async with session.lock:
            if session.tunnel is not None:
                await self.tunnel_broker.release(session.tunnel)
            summary = await self._session_summary(session, status="closed", live=False)
            await self._stop_trace_recording(session)
            # Detach network inspector before closing context
            if session.network_inspector is not None:
                session.network_inspector.detach()
                session.network_inspector = None
            try:
                await session.context.close()
            finally:
                if session.browser is not None and session.browser is not self.browser:
                    try:
                        await session.browser.close()
                    except Exception as exc:  # pragma: no cover - best effort isolated cleanup
                        logger.warning("failed to close isolated browser for session %s: %s", session_id, exc)
                if session.runtime is not None:
                    await self.runtime_provisioner.release(session.runtime)
            self.sessions.pop(session_id, None)
            if self._session_closed_hook is not None:
                try:
                    await self._session_closed_hook(session_id)
                except Exception as exc:
                    logger.warning("session closed hook failed for %s: %s", session_id, exc)
            await self.audit.append(
                event_type="session_closed",
                status="ok",
                action="close_session",
                session_id=session.id,
                details={
                    "trace_path": str(session.trace_path),
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                },
            )
            await self._record_witness_receipt(
                session,
                event_type="session",
                status="ok",
                action="close_session",
                action_class="control",
                metadata={
                    "trace_path": str(session.trace_path),
                    "isolation_mode": session.isolation_mode,
                    "browser_node": session.browser_node_name,
                },
            )
            summary["witness_remote"] = session.witness_remote_state.model_dump()
            await self.session_store.upsert(SessionRecord.model_validate(summary))
            return {"closed": True, "trace_path": str(session.trace_path), "session": summary}

    async def _maybe_provision_session_tunnel(self, session: BrowserSession) -> None:
        if session.isolation_mode != "docker_ephemeral" or session.runtime is None:
            return
        if not self.tunnel_broker.enabled:
            return
        if session.runtime.novnc_port is None or not self._takeover_url_is_local_only(session.takeover_url):
            return
        try:
            session.tunnel = await self.tunnel_broker.provision(
                session.id,
                local_host=session.runtime.tunnel_local_host,
                local_port=session.runtime.tunnel_local_port,
            )
            session.tunnel_error = None
        except Exception as exc:
            session.tunnel = None
            session.tunnel_error = "isolated tunnel provisioning failed"
            logger.warning("failed to provision isolated tunnel for session %s: %s", session.id, exc)

    async def _run_action(
        self,
        session: BrowserSession,
        action_name: str,
        target: dict[str, Any],
        operation,
    ) -> dict[str, Any]:
        return await self.action_pipeline.run(
            ActionRunContext(
                manager=self,
                session=session,
                action_name=action_name,
                target=target,
                operation=operation,
            )
        )


    # Known bot challenge URL patterns and page signals
    _BOT_CHALLENGE_SIGNALS = [
        "challenge.cloudflare.com",
        "challenges.cloudflare.com",
        "/cdn-cgi/challenge-platform/",
        "captcha",
        "recaptcha",
        "hcaptcha",
        "arkose",
        "unusual activity",
        "suspicious activity",
        "verify you're human",
        "verify you are human",
        "security check",
        "access denied",
        "bot detected",
    ]

    async def _check_bot_challenge(self, session: BrowserSession) -> dict[str, Any] | None:
        """Return a takeover payload if a bot challenge is detected, else None."""
        url = session.page.url.lower()
        title = ""
        body_text = ""
        iframe_sources: list[str] = []
        try:
            title = (await session.page.title()).lower()
            body_text = (await session.page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''")).lower()
            iframe_sources = [
                item.lower()
                for item in (
                    await session.page.evaluate(
                        "() => Array.from(document.querySelectorAll('iframe')).map((el) => el.src || el.getAttribute('src') || '')"
                    )
                )
            ]
        except Exception:
            pass

        combined = f"{url} {title} {body_text} {' '.join(iframe_sources)}"
        for signal in self._BOT_CHALLENGE_SIGNALS:
            if signal in combined:
                return {
                    "bot_challenge_detected": True,
                    "signal": signal,
                    "url": session.page.url,
                    "title": title,
                    "iframes": iframe_sources[:10],
                }
        return None

    async def _observation_payload(
        self,
        session: BrowserSession,
        *,
        limit: int = 40,
        screenshot_label: str = "observe",
        preset: str = "normal",
    ) -> dict[str, Any]:
        screenshot = await self._capture_screenshot(session, screenshot_label)

        # fast preset: screenshot only — skip OCR and accessibility tree
        if preset == "fast":
            title = await session.page.title()
            tabs = await self._tab_summaries(session)
            return {
                "session": await self._session_summary(session),
                "url": session.page.url,
                "title": title,
                "active_element": None,
                "text_excerpt": "",
                "dom_outline": {},
                "accessibility_outline": {"available": False, "nodes": []},
                "ocr": None,
                "interactables": [],
                "screenshot_path": screenshot["path"],
                "screenshot_url": screenshot["url"],
                "console_messages": session.console_messages[-10:],
                "page_errors": session.page_errors[-10:],
                "request_failures": [],
                "tabs": tabs,
                "recent_downloads": session.downloads[-10:],
                "takeover_url": self._current_takeover_url(session),
                "remote_access": self._session_remote_access_info(session),
                "preset": "fast",
            }

        # normal and rich share the same path; rich uses a larger text/interactable limit
        effective_limit = min(limit * 2, 200) if preset == "rich" else limit
        interactables = await session.page.evaluate(INTERACTABLES_SCRIPT, effective_limit)
        text_limit = 4000 if preset == "rich" else 2000
        summary = await self._page_summary(session.page, text_limit=text_limit)
        ocr = await self.ocr.extract_from_image(screenshot["path"])
        # Apply PII pixel-redaction on the already-captured screenshot in-place
        if self.pii_scrubber.screenshot_enabled and ocr and ocr.get("blocks"):
            try:
                scrubbed_path = Path(screenshot["path"])
                raw_bytes = scrubbed_path.read_bytes()
                scrubbed_bytes, hits = self.pii_scrubber.screenshot(raw_bytes, ocr["blocks"])
                if hits:
                    scrubbed_path.write_bytes(scrubbed_bytes)
                    if self.pii_scrubber.audit_report:
                        await self.audit.append(
                            event_type="pii_redaction",
                            status="ok",
                            action="screenshot_scrub",
                            session_id=session.id,
                            details=self.pii_scrubber.build_audit_report(
                                session.id, "screenshot", hits
                            ),
                        )
            except Exception as exc:
                logger.warning("screenshot PII redaction error for session %s: %s", session.id, exc)
        tabs = await self._tab_summaries(session)
        return {
            "session": await self._session_summary(session),
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "ocr": ocr,
            "interactables": interactables,
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
            "console_messages": session.console_messages[-10:],
            "page_errors": session.page_errors[-10:],
            "request_failures": session.request_failures[-10:],
            "tabs": tabs,
            "recent_downloads": session.downloads[-10:],
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "preset": preset,
        }

    async def _light_snapshot(self, session: BrowserSession, *, label: str) -> dict[str, Any]:
        screenshot = await self._capture_screenshot(session, label)
        summary = await self._page_summary(session.page)
        return {
            "url": session.page.url,
            "title": summary["title"],
            "active_element": summary["active_element"],
            "text_excerpt": summary["text_excerpt"],
            "dom_outline": summary["dom_outline"],
            "accessibility_outline": summary["accessibility_outline"],
            "screenshot_path": screenshot["path"],
            "screenshot_url": screenshot["url"],
        }

    async def _capture_screenshot(self, session: BrowserSession, label: str) -> dict[str, str]:
        return await self.artifacts.capture_screenshot(session, label)

    def _trace_payload(self, session: BrowserSession) -> dict[str, Any]:
        return self.artifacts.trace_payload(session)

    async def _stop_trace_recording(self, session: BrowserSession) -> None:
        if not self.settings.enable_tracing or not session.trace_recording:
            session.trace_recording = False
            return
        try:
            await session.context.tracing.stop(path=str(session.trace_path))
            session.trace_recording = False
        except Exception as exc:  # pragma: no cover - depends on external browser support
            logger.warning("failed to stop tracing for session %s: %s", session.id, exc)

    async def _page_summary(self, page: Page, text_limit: int = 2000) -> dict[str, Any]:
        summary = await page.evaluate(PAGE_SUMMARY_SCRIPT, text_limit)
        accessibility_outline = await self._accessibility_outline(page)
        return {
            "title": await page.title(),
            "active_element": await page.evaluate(ACTIVE_ELEMENT_SCRIPT),
            "text_excerpt": summary.get("text_excerpt", ""),
            "dom_outline": summary.get("dom_outline", {}),
            "accessibility_outline": accessibility_outline,
        }

    async def _accessibility_outline(self, page: Page) -> dict[str, Any]:
        accessibility = getattr(page, "accessibility", None)
        if accessibility is None or not hasattr(accessibility, "snapshot"):
            return {
                "available": False,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
            }

        try:
            snapshot = await accessibility.snapshot(interesting_only=True)
        except Exception as exc:
            logger.debug("failed to capture accessibility snapshot: %s", exc)
            return {
                "available": False,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
                "error": "accessibility_snapshot_unavailable",
            }

        if not snapshot:
            return {
                "available": True,
                "root_role": None,
                "root_name": None,
                "focused": None,
                "role_counts": {},
                "nodes": [],
            }

        nodes: list[dict[str, Any]] = []
        role_counts: dict[str, int] = {}
        focused: dict[str, Any] | None = None

        def walk(node: dict[str, Any], depth: int) -> None:
            nonlocal focused
            if len(nodes) >= ACCESSIBILITY_NODE_LIMIT:
                return
            role = node.get("role")
            if isinstance(role, str) and role:
                role_counts[role] = role_counts.get(role, 0) + 1
            compact = {
                "role": role,
                "name": node.get("name"),
                "value": node.get("valueString") or node.get("value"),
                "description": node.get("description"),
                "focused": bool(node.get("focused")),
                "disabled": bool(node.get("disabled")),
                "selected": bool(node.get("selected")),
                "checked": node.get("checked"),
                "expanded": node.get("expanded"),
                "pressed": node.get("pressed"),
                "depth": depth,
            }
            nodes.append(compact)
            if compact["focused"] and focused is None:
                focused = compact
            for child in node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                walk(child, depth + 1)
                if len(nodes) >= ACCESSIBILITY_NODE_LIMIT:
                    return

        walk(snapshot, 0)
        return {
            "available": True,
            "root_role": snapshot.get("role"),
            "root_name": snapshot.get("name"),
            "focused": focused,
            "role_counts": role_counts,
            "nodes": nodes,
        }

    def _session_auth_state_info(self, session: BrowserSession) -> dict[str, Any]:
        info = self.auth_state.inspect(session.last_auth_state_path)
        info["session_auth_root"] = str(session.auth_dir)
        info["profile_name"] = session.auth_profile_name
        return info

    async def get_auth_state_info(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is not None:
            return self._session_auth_state_info(session)
        record = await self.session_store.get(session_id)
        return record.auth_state

    async def list_audit_events(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        events = await self.audit.list(
            limit=limit,
            session_id=session_id,
            event_type=event_type,
            operator_id=operator_id,
        )
        return [item.model_dump() for item in events]

    async def list_witness_receipts(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        receipts = await self.witness.list(session_id, limit=limit)
        return [item.model_dump() for item in receipts]

    def _initial_witness_remote_state(self, protection_mode: str) -> WitnessRemoteState:
        configured = bool(self.settings.witness_enabled and self.witness_remote.enabled)
        return WitnessRemoteState(
            configured=configured,
            required=self._witness_remote_required_for_profile(protection_mode),
            tenant_id=self.settings.witness_remote_tenant_id,
            status="idle" if configured else "disabled",
        )

    def _witness_remote_required_for_profile(self, protection_mode: str) -> bool:
        return bool(
            self.settings.witness_enabled
            and protection_mode == "confidential"
            and self.settings.witness_remote_required_for_confidential
        )

    async def _ensure_witness_remote_ready(self, session: BrowserSession, *, action: str) -> None:
        if not session.witness_remote_state.required:
            return
        checked_at = utc_now()
        if not self.witness_remote.enabled:
            session.witness_remote_state.status = "failed"
            session.witness_remote_state.last_checked_at = checked_at
            session.witness_remote_state.last_error = (
                "Confidential session requires hosted Witness delivery, but WITNESS_REMOTE_URL is not configured."
            )
            raise PermissionError(session.witness_remote_state.last_error)
        try:
            await self.witness_remote.healthz()
        except Exception as exc:
            session.witness_remote_state.status = "failed"
            session.witness_remote_state.last_checked_at = checked_at
            session.witness_remote_state.last_error = (
                f"Hosted Witness preflight failed before {action}."
            )
            raise PermissionError(session.witness_remote_state.last_error) from exc
        session.witness_remote_state.status = "healthy"
        session.witness_remote_state.last_checked_at = checked_at
        session.witness_remote_state.last_error = None

    def _auth_material_encryption_ready(self) -> bool:
        return bool(self.auth_state.require_encryption or self.auth_state.encryption_enabled)

    def _witness_session_context(self, session: BrowserSession) -> WitnessSessionContext:
        return WitnessSessionContext(
            session_id=session.id,
            profile=session.protection_mode,  # type: ignore[arg-type]
            isolation_mode=session.isolation_mode,
            shared_takeover_surface=session.shared_takeover_surface,
            shared_browser_process=session.shared_browser_process,
            auth_state_encrypted=self._auth_material_encryption_ready(),
            operator=get_current_operator(),
        )

    async def _record_witness_receipt(
        self,
        session: BrowserSession,
        *,
        event_type: str,
        status: str,
        action: str,
        action_class: str,
        risk_category: str | None = None,
        target: dict[str, Any] | None = None,
        outcome: WitnessPolicyOutcome | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        approval: WitnessApproval | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.witness_enabled:
            return
        policy = outcome or WitnessPolicyOutcome(profile=session.protection_mode)  # type: ignore[arg-type]
        payload = {
            "profile": session.protection_mode,  # type: ignore[arg-type]
            "event_type": event_type,
            "status": status,
            "action": action,
            "action_class": action_class,  # type: ignore[arg-type]
            "session_id": session.id,
            "risk_category": risk_category,
            "operator": get_current_operator(),
            "approval": approval or WitnessApproval(),
            "target": self.witness_policy.redact_target(target or {}, evidence_mode=policy.evidence_mode),
            "concerns": policy.concerns,
            "evidence_mode": policy.evidence_mode,
            "evidence": WitnessEvidence(
                before=before if policy.evidence_mode == "standard" else None,
                after=after if policy.evidence_mode == "standard" else None,
                verification=verification,
                artifacts={},
            ),
            "metadata": metadata or {},
        }
        recorded = await self.witness.record(session.id, **payload)
        if not self.witness_remote.enabled:
            return
        attempted_at = utc_now()
        try:
            await self.witness_remote.record(
                session.id,
                recorded.model_dump(
                    mode="json",
                    exclude={"receipt_id", "scope", "chain_prev_hash", "chain_hash"},
                ),
            )
        except Exception as exc:
            session.witness_remote_state.status = "failed"
            session.witness_remote_state.last_attempted_at = attempted_at
            session.witness_remote_state.last_error = f"Hosted Witness delivery failed for {action}."
            logger.warning(
                "witness remote delivery failed for session %s action %s: %s",
                session.id,
                action,
                exc,
            )
            return
        session.witness_remote_state.status = "delivered"
        session.witness_remote_state.last_attempted_at = attempted_at
        session.witness_remote_state.last_delivered_at = attempted_at
        session.witness_remote_state.last_error = None

    async def _record_session_witness_receipt(
        self,
        session: BrowserSession,
        *,
        action: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.witness_enabled:
            return
        outcome = self.witness_policy.evaluate_session(self._witness_session_context(session))
        await self._record_witness_receipt(
            session,
            event_type="session",
            status=status,
            action=action,
            action_class="control",
            outcome=outcome,
            metadata=metadata,
        )

    def _witness_action_class(self, action_name: str, *, risk_category: str | None = None) -> str:
        if risk_category in {"payment", "account_change", "destructive"}:
            return risk_category
        if action_name == "upload":
            return "upload"
        if action_name in {"save_auth_profile", "save_storage_state"}:
            return "auth"
        if action_name in {"request_human_takeover", "close_session", "create_session"}:
            return "control"
        if action_name in {"navigate", "hover", "scroll", "wait", "reload", "go_back", "go_forward"}:
            return "read"
        return "write"

    def _consume_witness_context(self, session: BrowserSession) -> dict[str, Any]:
        payload = dict(session.pending_witness_context or {})
        session.pending_witness_context = None
        return payload

    def _build_witness_action_context(
        self,
        *,
        action_name: str,
        target: dict[str, Any],
        witness_context: dict[str, Any],
    ) -> WitnessActionContext:
        risk_category = witness_context.get("risk_category")
        action_class = self._witness_action_class(action_name, risk_category=risk_category)
        sensitive_input = bool(
            witness_context.get("sensitive_input")
            or target.get("text_redacted")
            or target.get("sensitive")
        )
        stores_auth_material = bool(
            witness_context.get("stores_auth_material")
            or action_name in {"save_auth_profile", "save_storage_state"}
        )
        return WitnessActionContext(
            action=action_name,
            action_class=action_class,  # type: ignore[arg-type]
            risk_category=risk_category,
            target=target,
            approval_id=(witness_context.get("approval_id") or target.get("approval_id")),
            approval_status=witness_context.get("approval_status"),
            sensitive_input=sensitive_input,
            stores_auth_material=stores_auth_material,
            runtime_requires_approval=bool(witness_context.get("runtime_requires_approval")),
        )

    @staticmethod
    def _action_verification(
        action_name: str,
        target: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        signals: list[str] = []
        if before.get("url") != after.get("url"):
            signals.append("url_changed")
        if before.get("title") != after.get("title"):
            signals.append("title_changed")
        if before.get("active_element") != after.get("active_element"):
            signals.append("active_element_changed")
        if before.get("text_excerpt") != after.get("text_excerpt"):
            signals.append("text_excerpt_changed")

        before_counts = (before.get("dom_outline") or {}).get("counts") or {}
        after_counts = (after.get("dom_outline") or {}).get("counts") or {}
        if before_counts != after_counts:
            signals.append("dom_counts_changed")

        before_accessibility = (before.get("accessibility_outline") or {}).get("focused")
        after_accessibility = (after.get("accessibility_outline") or {}).get("focused")
        if before_accessibility != after_accessibility:
            signals.append("accessibility_focus_changed")

        interacted_element = target.get("element_id")
        selector = target.get("selector")
        interactables = after.get("interactables") or []
        target_seen_after = None
        if interacted_element:
            target_seen_after = any(item.get("element_id") == interacted_element for item in interactables)
        elif selector:
            target_seen_after = any(item.get("selector_hint") == selector for item in interactables)

        if target_seen_after is True:
            signals.append("target_still_visible")
        elif target_seen_after is False:
            signals.append("target_no_longer_visible")

        verified = bool(signals)
        if action_name == "navigate":
            verified = "url_changed" in signals or "title_changed" in signals
        elif action_name in {"go_back", "go_forward"}:
            verified = "url_changed" in signals or "title_changed" in signals
        elif action_name in {
            "click",
            "press",
            "scroll",
        }:
            verified = bool(
                {
                    "url_changed",
                    "title_changed",
                    "active_element_changed",
                    "text_excerpt_changed",
                    "accessibility_focus_changed",
                }
                & set(signals)
            )
        elif action_name == "hover":
            verified = bool(
                {"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals)
            ) or target_seen_after is not None
        elif action_name in {"type", "select_option"}:
            verified = bool({"active_element_changed", "text_excerpt_changed", "accessibility_focus_changed"} & set(signals))
        elif action_name in {"wait", "reload"}:
            verified = True
        elif action_name == "upload":
            verified = True

        return {
            "verified": verified,
            "signals": signals,
            "target_seen_after": target_seen_after,
        }

    async def _session_summary(
        self,
        session: BrowserSession,
        *,
        status: SessionStatus = "active",
        live: bool = True,
    ) -> dict[str, Any]:
        return {
            "id": session.id,
            "name": session.name,
            "created_at": session.created_at.isoformat(),
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": status,
            "live": live,
            "current_url": session.page.url,
            "title": await session.page.title(),
            "artifact_dir": str(session.artifact_dir),
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "isolation": self._session_isolation(session),
            "auth_state": self._session_auth_state_info(session),
            "downloads": session.downloads[-20:],
            "last_action": session.last_action,
            "trace_path": str(session.trace_path),
            "proxy_persona": session.proxy_persona,
            "protection_mode": session.protection_mode,
            "witness_remote": session.witness_remote_state.model_dump(),
        }

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Public API for getting a session summary by ID."""
        session = await self.get_session(session_id)
        return await self._session_summary(session)

    async def _persist_session(self, session: BrowserSession, *, status: SessionStatus) -> None:
        summary = await self._session_summary(
            session,
            status=status,
            live=status == "active",
        )
        await self.session_store.upsert(SessionRecord.model_validate(summary))

    def _tab_pages(self, session: BrowserSession) -> list[Page]:
        return self.tabs.pages(session)

    async def _tab_summaries(self, session: BrowserSession) -> list[dict[str, Any]]:
        return await self.tabs.summaries(session)

    async def _settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.settings.action_timeout_ms, 5000))
        except Exception:
            pass
        await page.wait_for_timeout(250)

    def _assert_runtime_url_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "data", "blob", ""}:
            return
        self._assert_url_allowed(url)

    @staticmethod
    def _session_auth_root_for(base_root: str, session_id: str) -> Path:
        return Path(base_root).resolve() / session_id

    @staticmethod
    def _session_upload_root_for(base_root: str, session_id: str) -> Path:
        return Path(base_root).resolve() / session_id

    def _session_auth_root(self, session_id: str) -> Path:
        return self._session_auth_root_for(self.settings.auth_root, session_id)

    def _session_upload_root(self, session_id: str) -> Path:
        return self._session_upload_root_for(self.settings.upload_root, session_id)

    def _auth_profile_root(self) -> Path:
        return self.auth_profiles.root()

    @staticmethod
    def _resolve_contained_path(root: Path, candidate_path: str | Path, *, allow_absolute: bool = False) -> Path:
        return BrowserAuthProfileService.resolve_contained_path(
            root,
            candidate_path,
            allow_absolute=allow_absolute,
        )

    @staticmethod
    def _normalize_auth_profile_name(profile_name: str) -> str:
        return BrowserAuthProfileService.normalize_name(profile_name)

    def _auth_profile_dir(self, profile_name: str, *, create: bool) -> Path:
        return self.auth_profiles.dir(profile_name, create=create)

    def _auth_profile_metadata_path(self, profile_name: str, *, create: bool) -> Path:
        return self.auth_profiles.metadata_path(profile_name, create=create)

    def _auth_profile_state_base_path(self, profile_name: str, *, create: bool) -> Path:
        return self.auth_profiles.state_base_path(profile_name, create=create)

    def _resolve_auth_profile_state_path(self, profile_name: str, *, must_exist: bool) -> Path:
        return self.auth_profiles.resolve_state_path(profile_name, must_exist=must_exist)

    def _read_auth_profile_metadata(self, profile_name: str) -> dict[str, Any]:
        return self.auth_profiles.read_metadata(profile_name)

    def _session_isolation(self, session: BrowserSession) -> dict[str, Any]:
        payload = {
            "mode": session.isolation_mode,
            "browser_node": session.browser_node_name,
            "shared_takeover_surface": session.shared_takeover_surface,
            "shared_browser_process": session.shared_browser_process,
            "max_live_sessions_per_browser_node": session.max_live_sessions_per_browser_node,
            "state_roots": {
                "artifact_dir": str(session.artifact_dir),
                "auth_dir": str(session.auth_dir),
                "upload_dir": str(session.upload_dir),
            },
        }
        if session.runtime is not None:
            payload["runtime"] = {
                "container_id": session.runtime.container_id,
                "container_name": session.runtime.container_name,
                "network": session.runtime.network_name,
                "profile_dir": str(session.runtime.profile_dir),
                "downloads_dir": str(session.runtime.downloads_dir),
                "ws_endpoint_file": str(session.runtime.ws_endpoint_file),
                "novnc_port": session.runtime.novnc_port,
                "vnc_port": session.runtime.vnc_port,
            }
        return payload

    async def _approval_observation(self, session: BrowserSession) -> dict[str, Any]:
        return {
            "url": session.page.url,
            "title": await session.page.title(),
            "takeover_url": self._current_takeover_url(session),
            "remote_access": self._session_remote_access_info(session),
            "isolation": self._session_isolation(session),
            "auth_state": self._session_auth_state_info(session),
            "last_action": session.last_action,
        }

    def _approval_kind_for_decision(self, decision: BrowserActionDecision) -> ApprovalKind | None:
        if decision.action == "upload":
            return "upload" if self.settings.require_approval_for_uploads else None
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return None

    @staticmethod
    def _governed_approval_kind_for_decision(decision: BrowserActionDecision) -> ApprovalKind | None:
        if decision.risk_category == "read":
            return None
        if decision.action == "upload" or decision.risk_category == "upload":
            return "upload"
        if decision.risk_category in {"post", "payment", "account_change", "destructive"}:
            return decision.risk_category
        return "write"

    @staticmethod
    def _action_class(action_name: str) -> str:
        if action_name in {
            "navigate",
            "hover",
            "scroll",
            "wait",
            "reload",
            "go_back",
            "go_forward",
        }:
            return "read"
        return "write"

    def _assert_url_allowed(self, url: str) -> None:
        host = urlparse(url).hostname
        if not host:
            raise PermissionError(f"Could not determine hostname for URL: {url}")
        patterns = self.settings.allowed_host_patterns
        if "*" in patterns:
            return
        if not patterns or patterns == ["*"]:
            return
        for pattern in patterns:
            normalized = pattern.removeprefix("*.")
            if fnmatch.fnmatch(host, pattern) or host == normalized or host.endswith(f".{normalized}"):
                return
        raise PermissionError(f"Host {host!r} is not allowlisted")

    def _resolve_target(
        self,
        *,
        selector: str | None = None,
        element_id: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict[str, Any]:
        if element_id:
            return {
                "mode": "selector",
                "element_id": element_id,
                "selector": f'[data-operator-id="{element_id}"]',
            }
        if selector:
            return {"mode": "selector", "selector": selector}
        if x is not None and y is not None:
            return {"mode": "coordinates", "x": x, "y": y}
        raise ValueError("Provide selector, element_id, or x+y coordinates")

    def _safe_upload_path(self, file_path: str, *, session: BrowserSession | None = None) -> Path:
        return self.uploads.safe_path(file_path, session=session)

    @staticmethod
    def _path_is_contained_by(candidate: Path, root: Path) -> bool:
        return BrowserUploadService.path_is_contained_by(candidate, root)

    def _safe_session_auth_path(
        self,
        session: BrowserSession,
        relative_path: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        return self.auth_profiles.safe_session_auth_path(session, relative_path, must_exist=must_exist)

    def _safe_auth_path(self, relative_path: str, must_exist: bool = False) -> Path:
        return self.auth_profiles.safe_auth_path(relative_path, must_exist=must_exist)

    def _attach_page_listeners(self, page: Page, session: BrowserSession) -> None:
        if not hasattr(page, "on"):
            return
        page_id = id(page)
        if page_id in session.attached_pages:
            return
        session.attached_pages.add(page_id)

        page.on("console", lambda message: self._bounded_append(
            session.console_messages,
            {
                "type": message.type,
                "text": message.text,
                "location": message.location,
            },
        ))
        page.on("pageerror", lambda error: self._bounded_append(session.page_errors, str(error)))
        page.on("requestfailed", lambda request: self._bounded_append(
            session.request_failures,
            {
                "url": request.url,
                "method": request.method,
                "failure": str(request.failure) if request.failure else None,
            },
        ))
        page.on("download", lambda download: asyncio.create_task(self._handle_download(session, download)))

    def _bounded_append(self, items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

    async def _handle_download(self, session: BrowserSession, download: Any) -> None:
        return await self.diagnostics.handle_download(session, download)

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        await self.artifacts.append_jsonl(path, payload)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        SessionArtifactService.append_text(path, text)

    # ── Screenshot diff ──────────────────────────────────────────────────────

    async def screenshot_diff(self, session_id: str) -> dict[str, Any]:
        """Capture a new screenshot and compare pixel-by-pixel with the previous one.

        On the first call (no prior screenshot exists), saves a baseline and returns
        {"baseline_captured": True} — navigate to a new state and call again to compare.
        """
        session = await self.get_session(session_id)
        async with session.lock:
            # Find any prior non-diff-b screenshot first
            artifact_dir = session.artifact_dir
            prior_shots = sorted(
                [p for p in artifact_dir.glob("*.png") if "diff-b" not in p.name],
                key=lambda p: p.stat().st_mtime,
            )

            # Capture current state
            new_shot = await self._capture_screenshot(session, "diff-b")

            if not prior_shots:
                # No baseline — the screenshot we just took becomes the reference.
                # Rename it so next call picks it up as the prior.
                baseline_path = artifact_dir / "screenshot-baseline.png"
                shutil.copy2(new_shot["path"], str(baseline_path))
                return {
                    "baseline_captured": True,
                    "baseline_url": f"/artifacts/{session_id}/screenshot-baseline.png",
                    "message": "Baseline saved. Navigate to a new state and call compare again to see the diff.",
                }

            prev_path = prior_shots[-1]
            prev_url = f"/artifacts/{session_id}/{prev_path.name}"
            return await asyncio.to_thread(
                self._compute_diff,
                str(prev_path),
                new_shot["path"],
                prev_url,
                new_shot["url"],
                session.artifact_dir,
            )

    @staticmethod
    def _compute_diff(
        a_path: str,
        b_path: str,
        a_url: str,
        b_url: str,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        try:
            from PIL import Image, ImageChops  # type: ignore[import]

            img_a = Image.open(a_path).convert("RGB")
            img_b = Image.open(b_path).convert("RGB")

            # Resize b to match a dimensions if they differ
            if img_a.size != img_b.size:
                img_b = img_b.resize(img_a.size, Image.LANCZOS)

            diff = ImageChops.difference(img_a, img_b)
            total_pixels = img_a.width * img_a.height

            # Count non-black pixels in the diff
            data = diff.tobytes()
            changed = sum(
                1 for i in range(0, len(data), 3)
                if data[i] > 8 or data[i + 1] > 8 or data[i + 2] > 8
            )
            changed_pct = round(changed / total_pixels * 100, 4) if total_pixels > 0 else 0.0

            # Save diff image
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            diff_filename = f"{ts}-diff.png"
            diff_path = artifact_dir / diff_filename
            diff.save(str(diff_path))
            diff_url = f"/artifacts/{artifact_dir.name}/{diff_filename}"

            return {
                "changed_pixels": changed,
                "changed_pct": changed_pct,
                "diff_url": diff_url,
                "diff_path": str(diff_path),
                "a_url": a_url,
                "b_url": b_url,
                "width": img_a.width,
                "height": img_a.height,
            }
        except Exception as exc:
            logger.warning("screenshot diff failed: %s", exc)
            return {
                "error": "screenshot_diff_failed",
                "changed_pixels": -1,
                "changed_pct": -1.0,
                "diff_url": None,
                "diff_path": None,
                "a_url": a_url,
                "b_url": b_url,
                "width": 0,
                "height": 0,
            }

    # ── Auth profile export / import ────────────────────────────────────────

    async def export_auth_profile(self, profile_name: str) -> dict[str, Any]:
        """Package an auth profile dir as a .tar.gz and return the artifact path."""
        return await self.auth_profiles.export(profile_name)

    @staticmethod
    def _write_tar(source_dir: Path, dest: Path) -> None:
        BrowserAuthProfileService.write_tar(source_dir, dest)

    @staticmethod
    def _safe_auth_archive_member_name(member_name: str):
        return BrowserAuthProfileService.safe_archive_member_name(member_name)

    async def import_auth_profile(self, archive_path: str, *, overwrite: bool = False) -> dict[str, Any]:
        """Extract a .tar.gz archive into the reusable auth profile root."""
        return await self.auth_profiles.import_profile(archive_path, overwrite=overwrite)
