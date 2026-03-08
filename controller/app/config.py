from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    browser_cdp_endpoint: str = Field("http://browser-node:9222", alias="BROWSER_CDP_ENDPOINT")
    browser_cdp_ws_endpoint_file: str = Field(
        "/data/browser-profile/browser-ws-endpoint.txt",
        alias="BROWSER_CDP_WS_ENDPOINT_FILE",
    )
    takeover_url: str = Field(
        "http://localhost:6080/vnc.html?autoconnect=true&resize=scale",
        alias="TAKEOVER_URL",
    )
    artifact_root: str = Field("/data/artifacts", alias="ARTIFACT_ROOT")
    upload_root: str = Field("/data/uploads", alias="UPLOAD_ROOT")
    auth_root: str = Field("/data/auth", alias="AUTH_ROOT")
    allowed_hosts: str = Field("example.com,localhost", alias="ALLOWED_HOSTS")
    default_viewport_width: int = Field(1600, alias="DEFAULT_VIEWPORT_WIDTH")
    default_viewport_height: int = Field(900, alias="DEFAULT_VIEWPORT_HEIGHT")
    connect_retries: int = Field(60, alias="CONNECT_RETRIES")
    connect_retry_delay_seconds: float = Field(1.0, alias="CONNECT_RETRY_DELAY_SECONDS")
    max_sessions: int = Field(1, alias="MAX_SESSIONS")
    require_approval_for_uploads: bool = Field(True, alias="REQUIRE_APPROVAL_FOR_UPLOADS")
    enable_tracing: bool = Field(True, alias="ENABLE_TRACING")
    typing_delay_ms: int = Field(20, alias="TYPING_DELAY_MS")
    action_timeout_ms: int = Field(15000, alias="ACTION_TIMEOUT_MS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def allowed_host_patterns(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
