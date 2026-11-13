from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GP_", env_file=".env", extra="ignore")

    api_key: SecretStr = Field(min_length=16)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    cookie_file: Path = Path("state/cookies.json")
    cookie_cache: Path = Path("state/cookie-cache")
    conversation_file: Path = Path("state/conversations.json")
    max_conversations: int = Field(default=256, ge=1)
    browser_profile: Path = Path("state/browser")
    browser_enabled: bool = True
    browser_headless: bool = True
    browser_model_map: dict[str, str] = Field(default_factory=dict)
    browser_selectors_file: Path | None = None
    browser_executable: str | None = None
    upstream_proxy: str | None = None
    request_timeout: float = Field(default=300, gt=0)
    preflight_timeout: float = Field(default=30, gt=0)
    queue_timeout: float = Field(default=30, gt=0)
    max_queue: int = Field(default=8, ge=0, le=100)
    max_request_bytes: int = Field(default=2_000_000, gt=0)
    max_output_chars: int = Field(default=1_000_000, gt=0)
