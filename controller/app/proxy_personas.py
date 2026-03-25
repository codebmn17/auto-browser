"""
proxy_personas.py — Named proxy configuration management.

Each proxy persona has a static server/credential set so that different
agent sessions can be assigned distinct IPs — preventing shared network
footprints that platforms use to link sessions and trigger bans.

Persona file format (JSON at PROXY_PERSONA_FILE path):
  {
    "my-us-east": {
      "server": "http://proxy.example.com:8080",
      "username": "user1",
      "password": "secret",
      "description": "US East Coast residential proxy"
    },
    "my-eu-west": {
      "server": "socks5://proxy2.example.com:1080",
      "username": "user2",
      "password": "secret2",
      "description": "EU West datacenter proxy"
    }
  }

API:
  list_personas()              → list of persona summaries (no passwords)
  get_persona(name)            → full persona with credentials
  set_persona(name, **kwargs)  → create or update a persona
  delete_persona(name)         → remove a persona
  resolve_proxy(name)          → {server, username, password} ready for Playwright
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = {"server"}
_PASSWORD_MASK = "[MASKED]"


class ProxyPersonaStore:
    """Read/write proxy personas from a JSON config file."""

    def __init__(self, file_path: str | Path | None):
        self._path = Path(file_path) if file_path else None
        self._cache: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path is None:
            return {}
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("proxy persona file is not a JSON object: %s", self._path)
                return {}
            return data
        except Exception as exc:
            logger.warning("failed to load proxy persona file %s: %s", self._path, exc)
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        if self._path is None:
            raise RuntimeError("No PROXY_PERSONA_FILE configured — cannot save proxy personas")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Public API ──────────────────────────────────────────────────────────

    def list_personas(self) -> list[dict[str, Any]]:
        """Return all personas with passwords masked."""
        data = self._load()
        return [
            {
                "name": name,
                "server": persona.get("server", ""),
                "username": persona.get("username"),
                "has_password": bool(persona.get("password")),
                "description": persona.get("description", ""),
            }
            for name, persona in data.items()
        ]

    def get_persona(self, name: str) -> dict[str, Any]:
        """Return a persona by name. Raises KeyError if not found."""
        data = self._load()
        if name not in data:
            raise KeyError(f"Proxy persona not found: {name!r}")
        persona = dict(data[name])
        persona["name"] = name
        return persona

    def set_persona(
        self,
        name: str,
        *,
        server: str,
        username: str | None = None,
        password: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create or update a proxy persona. Returns the new summary."""
        if not name or not name.strip():
            raise ValueError("Persona name cannot be empty")
        if not server:
            raise ValueError("Persona server is required")
        data = self._load()
        data[name] = {
            "server": server,
            "username": username,
            "password": password,
            "description": description,
        }
        self._save(data)
        return {
            "name": name,
            "server": server,
            "username": username,
            "has_password": bool(password),
            "description": description,
        }

    def delete_persona(self, name: str) -> bool:
        """Delete a persona. Returns True if deleted, False if not found."""
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True

    def resolve_proxy(self, name: str) -> dict[str, Any]:
        """Return proxy config ready for Playwright context creation.

        Returns: {"server": str, "username": str | None, "password": str | None}
        """
        persona = self.get_persona(name)
        result: dict[str, Any] = {"server": persona["server"]}
        if persona.get("username"):
            result["username"] = persona["username"]
        if persona.get("password"):
            result["password"] = persona["password"]
        return result
