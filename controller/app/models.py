from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    name: str | None = None
    start_url: str | None = None
    storage_state_path: str | None = None


class ClickRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    x: float | None = None
    y: float | None = None


class TypeRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    text: str
    clear_first: bool = True


class PressRequest(BaseModel):
    key: str


class ScrollRequest(BaseModel):
    delta_x: float = 0
    delta_y: float = 600


class NavigateRequest(BaseModel):
    url: str


class UploadRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    file_path: str
    approved: bool = False


class SaveStorageStateRequest(BaseModel):
    path: str = Field(description="Relative path inside /data/auth")


class HumanTakeoverRequest(BaseModel):
    reason: str = "Manual review requested"


class SessionEnvelope(BaseModel):
    session: dict[str, Any]


class ActionEnvelope(BaseModel):
    action: str
    session: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    target: dict[str, Any]
