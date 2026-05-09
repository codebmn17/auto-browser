from __future__ import annotations

from .actions import BrowserActionService
from .auth_profiles import BrowserAuthProfileService
from .bot_challenge import BrowserBotChallengeService
from .diagnostics import BrowserDiagnosticsService
from .observation import BrowserObservationService
from .remote_access import BrowserRemoteAccessService
from .sessions import BrowserSessionService
from .tabs import BrowserTabService
from .uploads import BrowserUploadService
from .witness import BrowserWitnessService

__all__ = [
    "BrowserActionService",
    "BrowserAuthProfileService",
    "BrowserBotChallengeService",
    "BrowserDiagnosticsService",
    "BrowserObservationService",
    "BrowserRemoteAccessService",
    "BrowserSessionService",
    "BrowserTabService",
    "BrowserUploadService",
    "BrowserWitnessService",
]
