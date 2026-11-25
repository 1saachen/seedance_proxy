from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEEDANCE_",
        hide_input_in_errors=True,
    )

    database_path: Path = Path("data/seedance.sqlite")
    registration_enabled: bool = False
    cookie_secure: bool = True
    ark_api_key: SecretStr | None = None
    video_daily_limit: int = Field(default=5, ge=1, le=1000)
    video_poll_seconds: int = Field(default=20, ge=0, le=300)
    ark_asset_access_key: SecretStr | None = None
    ark_asset_secret_key: SecretStr | None = None
    ark_asset_project: str = Field(default="default", min_length=1, max_length=64)
