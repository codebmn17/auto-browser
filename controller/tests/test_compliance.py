from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.compliance import VALID_TEMPLATES, apply_compliance_template, write_compliance_manifest


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.require_auth_state_encryption = False
    settings.require_operator_id = False
    settings.pii_scrub_enabled = False
    settings.pii_scrub_screenshot = False
    settings.pii_scrub_network = False
    settings.pii_scrub_console = False
    settings.require_approval_for_uploads = False
    settings.witness_enabled = False
    settings.auth_state_max_age_hours = 72.0
    settings.session_isolation_mode = "shared_browser_node"
    return settings


@pytest.mark.parametrize("template", sorted(VALID_TEMPLATES))
def test_all_templates_apply(template: str) -> None:
    settings = _settings()

    overrides = apply_compliance_template(settings, template)

    assert isinstance(overrides, dict)
    assert overrides


def test_hipaa_sets_encryption() -> None:
    settings = _settings()
    apply_compliance_template(settings, "HIPAA")
    assert settings.require_auth_state_encryption is True


def test_hipaa_sets_isolation() -> None:
    settings = _settings()
    apply_compliance_template(settings, "HIPAA")
    assert settings.session_isolation_mode == "docker_ephemeral"


def test_hipaa_short_max_age() -> None:
    settings = _settings()
    apply_compliance_template(settings, "HIPAA")
    assert settings.auth_state_max_age_hours == 4.0


def test_invalid_template_raises() -> None:
    settings = _settings()
    with pytest.raises(ValueError, match="Unknown compliance template"):
        apply_compliance_template(settings, "FAKECOMPLIANCE")


def test_case_insensitive() -> None:
    settings = _settings()
    overrides = apply_compliance_template(settings, "hipaa")
    assert overrides


def test_write_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"

    write_compliance_manifest(
        template_name="SOC2",
        overrides={"pii_scrub_enabled": True},
        output_path=manifest_path,
    )

    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["template"] == "SOC2"
    assert "applied_overrides" in data
