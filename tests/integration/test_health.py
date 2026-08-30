from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import vendorguard.app as app_module
from vendorguard.app import create_app


def test_health_returns_ok() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_lifespan_manages_database_resources(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = Mock()
    engine.dispose = AsyncMock()
    session_factory = Mock()

    create_engine = Mock(return_value=engine)
    create_sessions = Mock(return_value=session_factory)

    monkeypatch.setattr(
        app_module,
        "create_database_engine",
        create_engine,
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "create_session_factory",
        create_sessions,
        raising=False,
    )

    app = create_app()

    with TestClient(app):
        assert app.state.database_engine is engine
        assert app.state.session_factory is session_factory

    create_engine.assert_called_once()
    create_sessions.assert_called_once_with(engine)
    engine.dispose.assert_awaited_once()
