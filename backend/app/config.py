from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .asset_presets import PresetAsset, PresetId
from .urls import is_public_https


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEEDANCE_",
        hide_input_in_errors=True,
    )

    database_path: Path = Path("data/seedance.sqlite")
    customer_demo_project_id: str | None = None
    customer_demo_database_path: Path | None = None
    registration_enabled: bool = False
    cookie_secure: bool = True
    ark_api_key: SecretStr | None = None
    video_daily_limit: int = Field(default=5, ge=1, le=1000)
    video_poll_seconds: int = Field(default=20, ge=0, le=300)
    video_callback_poll_seconds: int = Field(default=5, ge=0, le=300)
    ark_asset_access_key: SecretStr | None = None
    ark_asset_secret_key: SecretStr | None = None
    ark_asset_project: str = Field(default="default", min_length=1, max_length=64)
    ark_preset_assets: dict[PresetId, PresetAsset] = Field(default_factory=dict)
    upload_directory: Path | None = None
    upload_public_origin: str = ""
    upload_link_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    upload_ttl_seconds: int = Field(default=172800, ge=60, le=604800)
    upload_cleanup_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("upload_directory", "customer_demo_database_path", mode="before")
    @classmethod
    def parse_optional_path(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("upload_public_origin")
    @classmethod
    def normalize_upload_origin(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not is_public_https(value) or "?" in value or "#" in value:
            raise ValueError("upload public origin must be a public HTTPS origin")
        parsed = urlsplit(value)
        if parsed.path not in ("", "/"):
            raise ValueError("upload public origin must have a root path")
        host = parsed.hostname.rstrip(".")
        return f"https://[{host}]" if ":" in host else f"https://{host}"
