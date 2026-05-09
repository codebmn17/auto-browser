from __future__ import annotations

from .actions import BrowserActionService
from .auth_profiles import BrowserAuthProfileService
from .diagnostics import BrowserDiagnosticsService
from .observation import BrowserObservationService
from .sessions import BrowserSessionService
from .tabs import BrowserTabService
from .uploads import BrowserUploadService
from .witness import BrowserWitnessService

__all__ = [
    "BrowserActionService",
    "BrowserAuthProfileService",
    "BrowserDiagnosticsService",
    "BrowserObservationService",
    "BrowserSessionService",
    "BrowserTabService",
    "BrowserUploadService",
    "BrowserWitnessService",
]
