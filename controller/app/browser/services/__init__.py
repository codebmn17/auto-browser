from __future__ import annotations

from .auth_profiles import BrowserAuthProfileService
from .diagnostics import BrowserDiagnosticsService
from .tabs import BrowserTabService
from .uploads import BrowserUploadService

__all__ = [
    "BrowserAuthProfileService",
    "BrowserDiagnosticsService",
    "BrowserTabService",
    "BrowserUploadService",
]
