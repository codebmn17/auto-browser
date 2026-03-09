from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar, Token
from pathlib import Path
from uuid import uuid4

from .models import AuditEvent, OperatorIdentity

_CURRENT_OPERATOR: ContextVar[OperatorIdentity] = ContextVar(
    "current_operator",
    default=OperatorIdentity(id="anonymous", source="anonymous"),
)


def set_current_operator(operator_id: str | None, *, name: str | None = None, source: str = "header") -> Token:
    identity = OperatorIdentity(
        id=(operator_id or "anonymous").strip() or "anonymous",
        name=(name or None),
        source=source if operator_id else "anonymous",
    )
    return _CURRENT_OPERATOR.set(identity)


def reset_current_operator(token: Token) -> None:
    _CURRENT_OPERATOR.reset(token)


def get_current_operator() -> OperatorIdentity:
    return _CURRENT_OPERATOR.get()


class AuditStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.events_path = self.root / "events.jsonl"
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def append(
        self,
        *,
        event_type: str,
        status: str,
        action: str | None = None,
        session_id: str | None = None,
        approval_id: str | None = None,
        job_id: str | None = None,
        operator: OperatorIdentity | None = None,
        details: dict | None = None,
    ) -> AuditEvent:
        async with self._lock:
            event = AuditEvent(
                id=uuid4().hex[:12],
                timestamp=self._timestamp(),
                event_type=event_type,
                status=status,
                action=action,
                session_id=session_id,
                approval_id=approval_id,
                job_id=job_id,
                operator=operator or get_current_operator(),
                details=details or {},
            )
            line = event.model_dump_json()
            await asyncio.to_thread(self._append_text, self.events_path, line + "\n")
            return event

    async def list(
        self,
        *,
        limit: int = 100,
        session_id: str | None = None,
        event_type: str | None = None,
        operator_id: str | None = None,
    ) -> list[AuditEvent]:
        return await asyncio.to_thread(
            self._list_sync,
            limit,
            session_id,
            event_type,
            operator_id,
        )

    def _list_sync(
        self,
        limit: int,
        session_id: str | None,
        event_type: str | None,
        operator_id: str | None,
    ) -> list[AuditEvent]:
        if not self.events_path.exists():
            return []
        events: list[AuditEvent] = []
        for raw in self.events_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            event = AuditEvent.model_validate_json(raw)
            if session_id and event.session_id != session_id:
                continue
            if event_type and event.event_type != event_type:
                continue
            if operator_id and event.operator.id != operator_id:
                continue
            events.append(event)
        events.sort(key=lambda item: (item.timestamp, item.id), reverse=True)
        return events[:limit]

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _timestamp() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
