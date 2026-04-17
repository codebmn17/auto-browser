from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.vision_target import VisionTargeter


def _make_targeter() -> VisionTargeter:
    return VisionTargeter(api_key="test-key", model="claude-haiku-4-5-20251001")


def _screenshot_path(tmp_path: Path) -> Path:
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return path


def _mock_async_client(response_payload: dict[str, object]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = response_payload
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


@pytest.mark.asyncio
async def test_find_element_success(tmp_path: Path) -> None:
    targeter = _make_targeter()
    shot = _screenshot_path(tmp_path)
    response_payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "x": 100,
                        "y": 200,
                        "found": True,
                        "confidence": 0.95,
                        "description": "Blue submit button",
                    }
                ),
            }
        ]
    }

    with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(response_payload)):
        result = await targeter.find_element(str(shot), "the blue submit button")

    assert result["found"] is True
    assert result["x"] == 100
    assert result["y"] == 200


@pytest.mark.asyncio
async def test_find_element_not_found(tmp_path: Path) -> None:
    targeter = _make_targeter()
    shot = _screenshot_path(tmp_path)
    response_payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "x": 0,
                        "y": 0,
                        "found": False,
                        "confidence": 0.0,
                        "description": "not visible",
                    }
                ),
            }
        ]
    }

    with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(response_payload)):
        result = await targeter.find_element(str(shot), "invisible element")

    assert result["found"] is False


@pytest.mark.asyncio
async def test_find_element_malformed_json(tmp_path: Path) -> None:
    targeter = _make_targeter()
    shot = _screenshot_path(tmp_path)
    response_payload = {"content": [{"type": "text", "text": "not json at all"}]}

    with patch("app.vision_target.httpx.AsyncClient", return_value=_mock_async_client(response_payload)):
        result = await targeter.find_element(str(shot), "something")

    assert result["found"] is False
    assert "parse error" in result["description"]


@pytest.mark.asyncio
async def test_find_element_missing_screenshot() -> None:
    targeter = _make_targeter()

    with pytest.raises(FileNotFoundError):
        await targeter.find_element("/nonexistent/path.png", "anything")


def test_from_settings_no_key() -> None:
    settings = SimpleNamespace(anthropic_api_key=None)

    result = VisionTargeter.from_settings(settings)

    assert result is None


def test_from_settings_defaults_to_vision_model() -> None:
    settings = SimpleNamespace(
        anthropic_api_key="sk-test",
        anthropic_base_url="https://api.anthropic.com",
        vision_model="claude-haiku-4-5-20251001",
    )

    result = VisionTargeter.from_settings(settings)

    assert result is not None
    assert result.model == "claude-haiku-4-5-20251001"


def test_from_settings_uses_default_model_when_vision_model_missing() -> None:
    settings = SimpleNamespace(
        anthropic_api_key="sk-test",
        anthropic_base_url="https://api.anthropic.com",
    )

    result = VisionTargeter.from_settings(settings)

    assert result is not None
    assert result.model == "claude-haiku-4-5-20251001"
