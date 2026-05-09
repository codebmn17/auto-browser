from __future__ import annotations

from .auth_profiles import BrowserAuthProfileService
from .diagnostics import BrowserDiagnosticsService
from .observation import BrowserObservationService
from .tabs import BrowserTabService
from .uploads import BrowserUploadService

__all__ = [
    "BrowserAuthProfileService",
    "BrowserDiagnosticsService",
    "BrowserObservationService",
    "BrowserTabService",
    "BrowserUploadService",
]
