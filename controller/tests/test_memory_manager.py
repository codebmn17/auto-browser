from __future__ import annotations

import pytest

from app.memory_manager import MemoryManager, MemoryProfile


@pytest.fixture
def mem(tmp_path):
    return MemoryManager(tmp_path / "memory")


@pytest.mark.asyncio
async def test_startup_creates_dir(mem, tmp_path) -> None:
    await mem.startup()
    assert (tmp_path / "memory").is_dir()


@pytest.mark.asyncio
async def test_save_and_get(mem) -> None:
    await mem.startup()
    profile = await mem.save(
        "test-profile",
        goal_summary="Login to dashboard",
        completed_steps=["navigated to /login", "entered credentials"],
        discovered_selectors={"login_btn": "#submit"},
    )
    assert profile.name == "test-profile"
    loaded = await mem.get("test-profile")
    assert loaded is not None
    assert loaded.goal_summary == "Login to dashboard"
    assert "login_btn" in loaded.discovered_selectors


@pytest.mark.asyncio
async def test_get_nonexistent(mem) -> None:
    await mem.startup()
    assert await mem.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_save_merges_steps(mem) -> None:
    await mem.startup()
    await mem.save("p", completed_steps=["step1"])
    await mem.save("p", completed_steps=["step2"])
    profile = await mem.get("p")
    assert "step1" in profile.completed_steps
    assert "step2" in profile.completed_steps


@pytest.mark.asyncio
async def test_list(mem) -> None:
    await mem.startup()
    await mem.save("alpha")
    await mem.save("beta")
    profiles = await mem.list()
    names = [profile["name"] for profile in profiles]
    assert "alpha" in names
    assert "beta" in names


@pytest.mark.asyncio
async def test_delete(mem) -> None:
    await mem.startup()
    await mem.save("to-delete")
    assert await mem.delete("to-delete") is True
    assert await mem.get("to-delete") is None


@pytest.mark.asyncio
async def test_delete_nonexistent(mem) -> None:
    await mem.startup()
    assert await mem.delete("ghost") is False


def test_to_system_prompt() -> None:
    profile = MemoryProfile(
        name="p",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        goal_summary="Do the thing",
        completed_steps=["step a"],
        discovered_selectors={"btn": "#submit"},
    )
    prompt = profile.to_system_prompt()
    assert "Do the thing" in prompt
    assert "step a" in prompt
    assert "#submit" in prompt
