from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VENDORGUARD_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "VendorGuard"
    environment: str = "development"
    debug: bool = False
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5433, ge=1, le=65535)
    db_name: str = "vendorguard"
    db_user: str = "vendorguard"
    db_password: SecretStr | None = None


def load_settings(*, env_file: str | Path | None = ".env") -> Settings:
    return Settings(_env_file=env_file)
