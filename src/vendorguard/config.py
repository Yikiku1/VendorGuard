from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """集中定义 VendorGuard 的环境配置与安全默认值。"""

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
    jwt_secret: SecretStr | None = None
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    demo_specialist_password: SecretStr | None = None
    demo_manager_password: SecretStr | None = None


def load_settings(*, env_file: str | Path | None = ".env") -> Settings:
    """从环境变量和可选的 dotenv 文件加载配置。"""

    return Settings(_env_file=env_file)
