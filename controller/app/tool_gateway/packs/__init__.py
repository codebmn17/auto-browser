"""Pack registration modules for MCP tools."""

from . import core, harness

__all__ = ["core", "harness"]


def register_all(registry, gateway) -> None:
    core.register(registry, gateway)
    harness.register(registry, gateway)
