from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from vendorguard.config import load_settings


def test_load_settings_uses_safe_defaults() -> None:
    settings = load_settings(env_file=None)

    assert settings.app_name == "VendorGuard"
    assert settings.environment == "development"
    assert settings.debug is False
    assert settings.db_host == "127.0.0.1"
    assert settings.db_port == 5433
    assert settings.db_name == "vendorguard"
    assert settings.db_user == "vendorguard"
    assert settings.db_password is None


def test_load_settings_defaults_retrieval_embedding_configuration() -> None:
    """M3 检索配置有可用默认值: 探针确认过的模型名与正文向量缓存位置."""

    settings = load_settings(env_file=None)

    assert settings.embedding_model == "qwen3.7-text-embedding"
    assert settings.embedding_cache_path == Path("data/retrieval/cache/embedding_v1.json")


def test_load_settings_reads_retrieval_embedding_overrides(monkeypatch: MonkeyPatch) -> None:
    """两个配置项都能被环境变量覆盖, 名字要与 .env.example 一致."""

    monkeypatch.setenv("VENDORGUARD_EMBEDDING_MODEL", "qwen3.7-other-embedding")
    monkeypatch.setenv("VENDORGUARD_EMBEDDING_CACHE_PATH", "logs/other_vectors.json")

    settings = load_settings(env_file=None)

    assert settings.embedding_model == "qwen3.7-other-embedding"
    assert settings.embedding_cache_path == Path("logs/other_vectors.json")


def test_load_settings_defaults_review_data_directory() -> None:
    """M4 本地审查记录目录有默认值: 演示证据落在 var/reviews, 不进 Git."""

    settings = load_settings(env_file=None)

    assert settings.review_data_dir == Path("var/reviews")


def test_load_settings_reads_review_data_directory_override(
    monkeypatch: MonkeyPatch,
) -> None:
    """本地审查目录可被环境变量覆盖, 名字要与 .env.example 一致."""

    monkeypatch.setenv("VENDORGUARD_REVIEW_DATA_DIR", "var/other-reviews")

    assert load_settings(env_file=None).review_data_dir == Path("var/other-reviews")


def test_load_settings_ignores_unprefixed_environment_variables(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEBUG", "release")

    settings = load_settings(env_file=None)

    assert settings.debug is False


def test_load_settings_reads_prefixed_environment_variables(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("VENDORGUARD_APP_NAME", "VendorGuard Test")
    monkeypatch.setenv("VENDORGUARD_ENVIRONMENT", "testing")
    monkeypatch.setenv("VENDORGUARD_DEBUG", "true")
    monkeypatch.setenv("VENDORGUARD_DB_HOST", "database.test")
    monkeypatch.setenv("VENDORGUARD_DB_PORT", "6543")
    monkeypatch.setenv("VENDORGUARD_DB_NAME", "vendorguard_test")
    monkeypatch.setenv("VENDORGUARD_DB_USER", "test_user")
    monkeypatch.setenv("VENDORGUARD_DB_PASSWORD", "test-secret")

    settings = load_settings(env_file=None)

    assert settings.app_name == "VendorGuard Test"
    assert settings.environment == "testing"
    assert settings.debug is True
    assert settings.db_host == "database.test"
    assert settings.db_port == 6543
    assert settings.db_name == "vendorguard_test"
    assert settings.db_user == "test_user"
    assert settings.db_password is not None
    assert settings.db_password.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_load_settings_rejects_invalid_database_port(monkeypatch: MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("VENDORGUARD_DB_PORT", port)

    with pytest.raises(ValidationError):
        load_settings(env_file=None)


def test_load_settings_reads_jwt_configuration(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VENDORGUARD_JWT_SECRET", "j" * 32)
    monkeypatch.setenv("VENDORGUARD_ACCESS_TOKEN_EXPIRE_MINUTES", "30")

    settings = load_settings(env_file=None)

    assert settings.jwt_secret is not None
    assert settings.jwt_secret.get_secret_value() == "j" * 32
    assert settings.access_token_expire_minutes == 30
    assert "j" * 32 not in repr(settings)


def test_load_settings_reads_demo_account_passwords(
    monkeypatch: MonkeyPatch,
) -> None:
    specialist_password = f"demo-specialist-{uuid4().hex}"
    manager_password = f"demo-manager-{uuid4().hex}"

    monkeypatch.setenv(
        "VENDORGUARD_DEMO_SPECIALIST_PASSWORD",
        specialist_password,
    )
    monkeypatch.setenv(
        "VENDORGUARD_DEMO_MANAGER_PASSWORD",
        manager_password,
    )

    settings = load_settings(env_file=None)

    assert settings.demo_specialist_password is not None
    assert settings.demo_manager_password is not None
    assert settings.demo_specialist_password.get_secret_value() == specialist_password
    assert settings.demo_manager_password.get_secret_value() == manager_password
    assert specialist_password not in repr(settings)
    assert manager_password not in repr(settings)
