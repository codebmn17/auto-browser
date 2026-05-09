from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ...downloads import DownloadCaptureService
from ...pii_scrub import PiiScrubber

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession


class BrowserDiagnosticsService:
    """Encapsulates diagnostics helpers and download persistence hooks."""

    def __init__(self, manager: Any, pii_scrubber: PiiScrubber, download_capture: DownloadCaptureService) -> None:
        self.manager = manager
        self.pii_scrubber = pii_scrubber
        self.download_capture = download_capture

    async def get_console_messages(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            messages = session.console_messages[-limit:]
            if self.pii_scrubber.console_enabled:
                messages, hits = self.pii_scrubber.console(messages)
                if hits and self.pii_scrubber.audit_report:
                    await self.manager.audit.append(
                        event_type="pii_redaction",
                        status="ok",
                        action="console_scrub",
                        session_id=session_id,
                        details=self.pii_scrubber.build_audit_report(session_id, "console", hits),
                    )
            return {
                "session": await self.manager._session_summary(session),
                "items": messages,
            }

    async def get_page_errors(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return {
                "session": await self.manager._session_summary(session),
                "items": session.page_errors[-limit:],
            }

    async def get_request_failures(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return {
                "session": await self.manager._session_summary(session),
                "items": session.request_failures[-limit:],
            }

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return list(session.downloads)
        record = await self.manager.session_store.get(session_id)
        return list(record.downloads)

    async def handle_download(self, session: "BrowserSession", download: Any) -> None:
        record = await self.download_capture.capture(session, download)
        await self.manager.audit.append(
            event_type="download_captured",
            status=record["status"],
            action="download",
            session_id=session.id,
            details={"filename": record["filename"], "url": record["url"], "failure": record["failure"]},
        )
        if session.id in self.manager.sessions:
            try:
                await self.manager._persist_session(session, status="active")
            except Exception as exc:
                logger.warning(
                    "failed to persist download metadata for session %s: %s",
                    session.id,
                    exc,
                )
