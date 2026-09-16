from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, Table
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.config import Settings
from vendorguard.security import (
    InvalidAccessTokenError,
    User,
    UserRole,
    authenticate_user,
    create_access_token,
    decode_access_token,
    hash_password,
    seed_demo_users,
    verify_password,
)


def _demo_password(label: str) -> str:
    """运行时生成测试口令, 使源码中不出现凭据形状的字面量。"""

    return f"demo-{label}-{uuid4().hex}"


def test_user_roles_match_mvp_contract() -> None:
    assert {role.value for role in UserRole} == {
        "procurement_specialist",
        "procurement_manager",
        "quality_manager",
        "admin",
    }


def test_user_model_has_mininum_authentication_fields() -> None:
    table = cast(Table, User.__table__)

    assert table.name == "users"
    assert set(table.c.keys()) == {
        "id",
        "username",
        "password_hash",
        "role",
        "is_active",
        "created_at",
    }
    assert tuple(column.name for column in table.primary_key.columns) == ("id",)
    assert table.c.username.unique is True
    assert table.c.username.nullable is False
    assert table.c.password_hash.nullable is False
    assert "password" not in table.c


def test_user_model_uses_stable_roles_and_database_defaults() -> None:
    table = cast(Table, User.__table__)
    role_type = table.c.role.type

    assert isinstance(role_type, SqlEnum)
    assert role_type.enums == [role.value for role in UserRole]
    assert role_type.native_enum is False
    assert role_type.create_constraint is False

    role_constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == "user_role"
    )
    constraint_sql = str(
        role_constraint.sqltext.compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )
    assert all(role.value in constraint_sql for role in UserRole)
    assert table.c.is_active.server_default is not None
    assert table.c.created_at.server_default is not None


def test_password_hash_uses_argon2_and_verifies_plaintext() -> None:
    password = _demo_password("local")

    password_hash = hash_password(password)

    assert password_hash != password
    assert password_hash.startswith("$argon2")
    assert verify_password(password, password_hash) is True
    assert verify_password(_demo_password("wrong"), password_hash) is False


def test_access_token_round_trip_contains_identity_and_expiry() -> None:
    settings = Settings(
        jwt_secret=SecretStr("j" * 32),
        access_token_expire_minutes=30,
    )
    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash="unused-in-token-test",
        role=UserRole.PROCUREMENT_SPECIALIST,
    )
    now = datetime.now(UTC).replace(microsecond=0)

    token = create_access_token(user, settings, now=now)
    claims = decode_access_token(token, settings)

    assert claims.user_id == user.id
    assert claims.username == user.username
    assert claims.role == user.role
    assert claims.expires_at == now + timedelta(minutes=30)


def test_decode_access_token_rejects_expired_token() -> None:
    settings = Settings(jwt_secret=SecretStr("j" * 32))
    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash="unused-in-token-test",
        role=UserRole.PROCUREMENT_SPECIALIST,
    )
    issued_at = datetime.now(UTC) - timedelta(minutes=31)
    token = create_access_token(user, settings, now=issued_at)

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, settings)


def test_decode_access_token_rejects_token_signed_with_another_secret() -> None:
    trusted_settings = Settings(jwt_secret=SecretStr("j" * 32))
    attacker_settings = Settings(jwt_secret=SecretStr("a" * 32))

    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash="unused-in-token-test",
        role=UserRole.PROCUREMENT_SPECIALIST,
    )

    forged_token = create_access_token(user, attacker_settings)

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(forged_token, trusted_settings)


def test_create_access_token_requires_jwt_secret() -> None:
    settings = Settings(jwt_secret=None)
    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash="unused-in-token-test",
        role=UserRole.PROCUREMENT_SPECIALIST,
    )

    with pytest.raises(RuntimeError, match="必须配置 JWT 密钥"):
        create_access_token(user, settings)


async def test_seed_demo_users_creates_the_demo_accounts() -> None:
    specialist_password = _demo_password("specialist")
    manager_password = _demo_password("manager")
    settings = Settings(
        demo_specialist_password=SecretStr(specialist_password),
        demo_manager_password=SecretStr(manager_password),
    )

    session = AsyncMock(spec=AsyncSession)
    existing_usernames = Mock()
    existing_usernames.all.return_value = []
    session.scalars.return_value = existing_usernames

    created_users = await seed_demo_users(session, settings)

    users_by_username = {user.username: user for user in created_users}

    assert set(users_by_username) == {
        "demo.specialist",
        "demo.manager",
        "admin",
    }
    assert users_by_username["demo.specialist"].role == UserRole.PROCUREMENT_SPECIALIST
    assert users_by_username["demo.manager"].role == UserRole.PROCUREMENT_MANAGER
    assert users_by_username["admin"].role == UserRole.ADMIN
    assert verify_password(
        specialist_password,
        users_by_username["demo.specialist"].password_hash,
    )
    assert verify_password(
        manager_password,
        users_by_username["demo.manager"].password_hash,
    )
    # 没单独配置管理员口令时沿用演示专员的口令 (演示时少记一个)
    assert verify_password(
        specialist_password,
        users_by_username["admin"].password_hash,
    )
    session.add_all.assert_called_once_with(created_users)


async def test_seed_demo_users_treats_a_blank_admin_password_as_unset() -> None:
    """VENDORGUARD_DEMO_ADMIN_PASSWORD 留空时视作未配置: 管理员沿用演示专员的口令."""

    specialist_password = _demo_password("specialist")
    settings = Settings(
        demo_specialist_password=SecretStr(specialist_password),
        demo_manager_password=SecretStr(_demo_password("manager")),
        demo_admin_password=SecretStr(""),
    )

    session = AsyncMock(spec=AsyncSession)
    existing_usernames = Mock()
    existing_usernames.all.return_value = []
    session.scalars.return_value = existing_usernames

    created_users = await seed_demo_users(session, settings)

    admin_user = next(user for user in created_users if user.username == "admin")
    assert verify_password(specialist_password, admin_user.password_hash)


async def test_seed_demo_users_uses_a_separate_admin_password_when_configured() -> None:
    """配置了 VENDORGUARD_DEMO_ADMIN_PASSWORD 时, 管理员用自己的口令."""

    admin_password = _demo_password("admin")
    settings = Settings(
        demo_specialist_password=SecretStr(_demo_password("specialist")),
        demo_manager_password=SecretStr(_demo_password("manager")),
        demo_admin_password=SecretStr(admin_password),
    )

    session = AsyncMock(spec=AsyncSession)
    existing_usernames = Mock()
    existing_usernames.all.return_value = []
    session.scalars.return_value = existing_usernames

    created_users = await seed_demo_users(session, settings)

    admin_user = next(user for user in created_users if user.username == "admin")
    assert admin_user.role == UserRole.ADMIN
    assert verify_password(admin_password, admin_user.password_hash)


async def test_seed_demo_users_requires_manager_password() -> None:
    settings = Settings(
        demo_specialist_password=SecretStr(_demo_password("specialist")),
        demo_manager_password=None,
    )

    session = AsyncMock(spec=AsyncSession)
    existing_usernames = Mock()
    existing_usernames.all.return_value = []
    session.scalars.return_value = existing_usernames

    with pytest.raises(
        RuntimeError,
        match="必须配置演示账号密码",
    ):
        await seed_demo_users(session, settings)

    session.scalars.assert_not_awaited()
    session.add_all.assert_not_called()


async def test_seed_demo_users_skips_existing_accounts() -> None:
    settings = Settings(
        demo_specialist_password=SecretStr(_demo_password("specialist")),
        demo_manager_password=SecretStr(_demo_password("manager")),
    )

    session = AsyncMock(spec=AsyncSession)
    existing_usernames = Mock()
    existing_usernames.all.return_value = [
        "demo.specialist",
        "demo.manager",
        "admin",
    ]
    session.scalars.return_value = existing_usernames

    created_users = await seed_demo_users(session, settings)

    assert created_users == []
    session.add_all.assert_not_called()


async def test_authenticate_user_accepts_valid_credentials() -> None:
    password = _demo_password("valid")
    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash=hash_password(password),
        role=UserRole.PROCUREMENT_SPECIALIST,
        is_active=True,
    )

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = user

    authenticated_user = await authenticate_user(
        session,
        username="demo.specialist",
        password=password,
    )

    assert authenticated_user is user
    session.scalar.assert_awaited_once()


async def test_authenticate_user_rejects_wrong_password() -> None:
    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash=hash_password(_demo_password("correct")),
        role=UserRole.PROCUREMENT_SPECIALIST,
        is_active=True,
    )

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = user

    authenticated_user = await authenticate_user(
        session,
        username="demo.specialist",
        password=_demo_password("wrong"),
    )

    assert authenticated_user is None
    session.scalar.assert_awaited_once()


async def test_authenticate_user_rejects_inactive_user() -> None:
    password = _demo_password("correct")
    user = User(
        id=uuid4(),
        username="demo.specialist",
        password_hash=hash_password(password),
        role=UserRole.PROCUREMENT_SPECIALIST,
        is_active=False,
    )

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = user

    authenticated_user = await authenticate_user(
        session,
        username="demo.specialist",
        password=password,
    )

    assert authenticated_user is None
    session.scalar.assert_awaited_once()
