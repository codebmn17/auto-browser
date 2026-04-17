from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.tool_gateway import McpToolGateway
from app.tool_inputs import GetStorageInput, SetStorageInput


def _build_gateway() -> tuple[McpToolGateway, SimpleNamespace, SimpleNamespace]:
    page = SimpleNamespace(evaluate=AsyncMock(return_value=None))
    session = SimpleNamespace(page=page)
    manager = SimpleNamespace(get_session=AsyncMock(return_value=session))
    gateway = McpToolGateway(
        manager=manager,
        orchestrator=SimpleNamespace(list_providers=lambda: []),
        job_queue=SimpleNamespace(),
        tool_profile="full",
    )
    return gateway, manager, page


def test_get_storage_local() -> None:
    payload = GetStorageInput(session_id="s1", storage_type="local")
    assert payload.storage_type == "local"


def test_get_storage_session() -> None:
    payload = GetStorageInput(session_id="s1", storage_type="session")
    assert payload.storage_type == "session"


def test_get_storage_invalid() -> None:
    with pytest.raises(ValidationError):
        GetStorageInput(session_id="s1", storage_type="constructor")


def test_set_storage_invalid() -> None:
    with pytest.raises(ValidationError):
        SetStorageInput(session_id="s1", key="k", value="v", storage_type="__proto__")


def test_set_storage_valid() -> None:
    payload = SetStorageInput(session_id="s1", key="k", value="v", storage_type="session")
    assert payload.storage_type == "session"


@pytest.mark.asyncio
async def test_get_storage_runtime_guard_rejects_invalid_storage_type() -> None:
    gateway, manager, _ = _build_gateway()
    payload = GetStorageInput.model_construct(session_id="s1", key="k", storage_type="constructor")

    with pytest.raises(ValueError, match="Invalid storage_type"):
        await gateway._get_local_storage(payload)

    manager.get_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_storage_runtime_guard_rejects_invalid_storage_type() -> None:
    gateway, manager, _ = _build_gateway()
    payload = SetStorageInput.model_construct(session_id="s1", key="k", value="v", storage_type="__proto__")

    with pytest.raises(ValueError, match="Invalid storage_type"):
        await gateway._set_local_storage(payload)

    manager.get_session.assert_not_awaited()
