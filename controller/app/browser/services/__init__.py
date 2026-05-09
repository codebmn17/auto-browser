from __future__ import annotations

from .actions import BrowserActionService
from .auth_profiles import BrowserAuthProfileService
from .diagnostics import BrowserDiagnosticsService
from .observation import BrowserObservationService
from .tabs import BrowserTabService
from .uploads import BrowserUploadService

__all__ = [
    "BrowserActionService",
    "BrowserAuthProfileService",
    "BrowserDiagnosticsService",
    "BrowserObservationService",
    "BrowserTabService",
    "BrowserUploadService",
]
