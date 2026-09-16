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
    # 演示与开发用的管理员账号 (admin)。不配置时与演示专员同密码, 少记一个口令;
    # 想给它单独的口令就在 .env 里设 VENDORGUARD_DEMO_ADMIN_PASSWORD。
    demo_admin_password: SecretStr | None = None

    # 制度检索的 embedding 配置: 模型名与正文向量缓存位置。
    # 缓存是调用付费接口算出的派生物, 不入库, 指纹不符时整份重建。
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_cache_path: Path = Path("data/retrieval/cache/embedding_v1.json")
    # M4 本地审查记录目录: 演示证据落在本地忽略目录, 不进公开仓库。
    review_data_dir: Path = Path("var/reviews")


def load_settings(*, env_file: str | Path | None = ".env") -> Settings:
    """从环境变量和可选的 dotenv 文件加载配置。"""

    return Settings(_env_file=env_file)
