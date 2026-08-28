import pytest
from pytest import MonkeyPatch
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from vendorguard.config import load_settings
from vendorguard.database import (
    Base,
    build_database_url,
    create_database_engine,
    create_session_factory,
)


def test_build_database_url_uses_settings(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VENDORGUARD_DB_HOST", "database.test")
    monkeypatch.setenv("VENDORGUARD_DB_PORT", "6543")
    monkeypatch.setenv("VENDORGUARD_DB_NAME", "vendorguard_test")
    monkeypatch.setenv("VENDORGUARD_DB_USER", "test_user")
    monkeypatch.setenv("VENDORGUARD_DB_PASSWORD", "test-secret")

    settings = load_settings(env_file=None)

    url = build_database_url(settings)

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "database.test"
    assert url.port == 6543
    assert url.database == "vendorguard_test"
    assert url.username == "test_user"
    assert url.password == "test-secret"
    assert "test-secret" not in str(url)


def test_build_database_url_requires_password(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("VENDORGUARD_BD_PASSWORD", raising=False)
    settings = load_settings(env_file=None)

    with pytest.raises(ValueError, match="Database password is required"):
        build_database_url(settings)


async def test_create_database_engine_uses_async_driver(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VENDORGUARD_DB_PASSWORD", "test-secret")
    settings = load_settings(env_file=None)

    engine = create_database_engine(settings)

    try:
        assert isinstance(engine, AsyncEngine)
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.url.host == "127.0.0.1"
        assert engine.url.port == 5433
        assert "test-secret" not in str(engine.url)
    finally:
        await engine.dispose()


async def test_create_session_factory_produces_async_sessions(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("VENDORGUARD_DB_PASSWORD", "test-secret")
    settings = load_settings(env_file=None)
    engine = create_database_engine(settings)

    session_factory = create_session_factory(engine)
    session = session_factory()

    try:
        assert isinstance(session, AsyncSession)
        assert session.sync_session.expire_on_commit is False
    finally:
        await session.close()
        await engine.dispose()


def test_declarative_base_exposes_metadata() -> None:
    assert isinstance(Base.metadata, MetaData)
