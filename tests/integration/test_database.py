import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.config import load_settings
from vendorguard.database import (
    create_database_engine,
    create_session_factory,
    transactional_session,
)


async def test_database_session_executes_query() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            result = await session.execute(text("SELECT 1"))

        assert result.scalar_one() == 1
    finally:
        await engine.dispose()


async def test_transactional_session_rolls_back_on_error() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    captured_session: AsyncSession | None = None

    try:
        with pytest.raises(RuntimeError, match="simulated failure"):
            async with transactional_session(session_factory) as session:
                captured_session = session
                await session.execute(text("SELECT 1"))
                assert session.in_transaction() is True

                raise RuntimeError("simulated failure")

        assert captured_session is not None
        assert captured_session.in_transaction() is False
    finally:
        await engine.dispose()


async def test_transactional_session_finishes_transaction_on_success() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    captured_session: AsyncSession | None = None

    try:
        async with transactional_session(session_factory) as session:
            captured_session = session
            result = await session.execute(text("SELECT 1"))

            assert result.scalar_one() == 1
            assert session.in_transaction() is True

        assert captured_session is not None
        assert captured_session.in_transaction() is False
    finally:
        await engine.dispose()
