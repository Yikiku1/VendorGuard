from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from vendorguard.app import create_app
from vendorguard.config import load_settings
from vendorguard.security import (
    User,
    UserRole,
    create_access_token,
    decode_access_token,
)


def test_login_returns_bearer_token_for_demo_specialist() -> None:
    settings = load_settings()
    specialist_password = settings.demo_specialist_password

    assert specialist_password is not None

    password = specialist_password.get_secret_value()

    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/login",
            json={
                "username": "demo.specialist",
                "password": password,
            },
        )

    assert response.status_code == 200

    payload = response.json()

    assert set(payload) == {"access_token", "token_type"}
    assert payload["token_type"] == "bearer"
    assert password not in response.text

    claims = decode_access_token(payload["access_token"], settings)

    assert claims.username == "demo.specialist"
    assert claims.role == UserRole.PROCUREMENT_SPECIALIST


def test_login_rejects_wrong_password_without_token() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/login",
            json={
                "username": "demo.specialist",
                "password": f"wrong-{uuid4().hex}",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "账号或密码错误",
    }
    assert response.headers["www-authenticate"] == "Bearer"
    assert "access_token" not in response.text


def test_login_does_not_reveal_unknown_username() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/login",
            json={
                "username": f"unknown-{uuid4().hex}",
                "password": f"irrelevant-{uuid4().hex}",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "账号或密码错误",
    }
    assert response.headers["www-authenticate"] == "Bearer"
    assert "access_token" not in response.text


def test_login_returns_manager_role_for_demo_manager() -> None:
    settings = load_settings()
    manager_password = settings.demo_manager_password

    assert manager_password is not None

    with TestClient(create_app()) as client:
        response = client.post(
            "/auth/login",
            json={
                "username": "demo.manager",
                "password": manager_password.get_secret_value(),
            },
        )

    assert response.status_code == 200

    payload = response.json()
    claims = decode_access_token(payload["access_token"], settings)

    assert claims.username == "demo.manager"
    assert claims.role == UserRole.PROCUREMENT_MANAGER


def test_current_user_returns_authenticated_specialist() -> None:
    settings = load_settings()
    specialist_password = settings.demo_specialist_password

    assert specialist_password is not None

    with TestClient(create_app()) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "demo.specialist",
                "password": specialist_password.get_secret_value(),
            },
        )

        assert login_response.status_code == 200

        access_token = login_response.json()["access_token"]

        response = client.get(
            "/auth/me",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
        )

    assert response.status_code == 200

    payload = response.json()

    assert set(payload) == {"id", "username", "role"}
    assert str(UUID(payload["id"])) == payload["id"]
    assert payload["username"] == "demo.specialist"
    assert payload["role"] == UserRole.PROCUREMENT_SPECIALIST.value


def test_current_user_rejects_forged_token() -> None:
    with TestClient(
        create_app(),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(
            "/auth/me",
            headers={
                "Authorization": "Bearer forged-token",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "无效访问令牌",
    }
    assert response.headers["www-authenticate"] == "Bearer"


def test_current_user_rejects_missing_bearer_token() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == {
        "detail": "无效访问令牌",
    }
    assert response.headers["www-authenticate"] == "Bearer"


def test_current_user_rejects_expired_token() -> None:
    settings = load_settings()
    specialist_password = settings.demo_specialist_password

    assert specialist_password is not None

    with TestClient(create_app()) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "demo.specialist",
                "password": specialist_password.get_secret_value(),
            },
        )

        assert login_response.status_code == 200

        claims = decode_access_token(
            login_response.json()["access_token"],
            settings,
        )
        user = User(
            id=claims.user_id,
            username=claims.username,
            password_hash="unused-in-token-test",
            role=claims.role,
        )
        issued_at = datetime.now(UTC) - timedelta(minutes=settings.access_token_expire_minutes + 1)
        expired_token = create_access_token(
            user,
            settings,
            now=issued_at,
        )

        response = client.get(
            "/auth/me",
            headers={
                "Authorization": f"Bearer {expired_token}",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "无效访问令牌",
    }
    assert response.headers["www-authenticate"] == "Bearer"


def test_current_user_rejects_token_for_missing_user() -> None:
    settings = load_settings()
    missing_user = User(
        id=uuid4(),
        username="missing.user",
        password_hash="unused-in-token-test",
        role=UserRole.PROCUREMENT_SPECIALIST,
    )
    access_token = create_access_token(missing_user, settings)

    with TestClient(create_app()) as client:
        response = client.get(
            "/auth/me",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "无效访问令牌",
    }
    assert response.headers["www-authenticate"] == "Bearer"
