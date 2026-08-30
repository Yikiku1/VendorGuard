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
    specialist_password = "specialist-demo-password"
    manager_password = "manager-demo-password"

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
