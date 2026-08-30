from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    String,
    func,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.config import Settings
from vendorguard.database import Base

_PASSWORD_HASH = PasswordHash.recommended()
_JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    """使用推荐的 Argon2 参数生成密码哈希。"""

    return _PASSWORD_HASH.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与已保存哈希是否匹配。"""

    try:
        return _PASSWORD_HASH.verify(password, password_hash)
    except UnknownHashError:
        return False


class UserRole(StrEnum):
    """定义数据库和权限逻辑共用的稳定用户角色。"""

    PROCUREMENT_SPECIALIST = "procurement_specialist"
    PROCUREMENT_MANAGER = "procurement_manager"
    QUALITY_MANAGER = "quality_manager"
    ADMIN = "admin"


_USER_ROLE_VALUES_SQL = ", ".join(f"'{role.value}'" for role in UserRole)


@dataclass(frozen=True)
class AccessTokenClaims:
    """表示经过验证的访问令牌身份声明。"""

    user_id: UUID
    username: str
    role: UserRole
    expires_at: datetime


class InvalidAccessTokenError(ValueError):
    """表示访问令牌无效、伪造或已经过期。"""

    pass


class User(Base):
    """保存登录身份、密码哈希、角色和启用状态。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            f"role IN ({_USER_ROLE_VALUES_SQL})",
            name="user_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [role.value for role in enum_type],
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


async def authenticate_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
) -> User | None:
    """验证启用用户的用户名和密码。失败时返回空值。"""

    user = await session.scalar(select(User).where(User.username == username))

    if user is None or not user.is_active:
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user


async def seed_demo_users(
    session: AsyncSession,
    settings: Settings,
) -> list[User]:
    """幂等创建采购专员和采购经理两个演示账号。"""

    if settings.demo_specialist_password is None or settings.demo_manager_password is None:
        raise RuntimeError("Demo account passwords are required")

    account_specs = (
        (
            "demo.specialist",
            UserRole.PROCUREMENT_SPECIALIST,
            settings.demo_specialist_password,
        ),
        (
            "demo.manager",
            UserRole.PROCUREMENT_MANAGER,
            settings.demo_manager_password,
        ),
    )
    usernames = tuple(username for username, _, _ in account_specs)

    existing_usernames = set(
        (await session.scalars(select(User.username).where(User.username.in_(usernames)))).all()
    )

    created_users = [
        User(
            username=username,
            password_hash=hash_password(password.get_secret_value()),
            role=role,
        )
        for username, role, password in account_specs
        if username not in existing_usernames
    ]

    if created_users:
        session.add_all(created_users)

    return created_users


def _get_jwt_secret(settings: Settings) -> str:
    """读取 JWT 密钥并在缺少配置时立即报错。"""

    if settings.jwt_secret is None:
        raise RuntimeError("JWT secret is required")

    return settings.jwt_secret.get_secret_value()


def create_access_token(
    user: User,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    """为已认证用户签发带身份、角色和过期时间的 JWT。"""

    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_expire_minutes)

    return jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.value,
            "iat": issued_at,
            "exp": expires_at,
        },
        _get_jwt_secret(settings),
        algorithm=_JWT_ALGORITHM,
    )


def decode_access_token(
    token: str,
    settings: Settings,
) -> AccessTokenClaims:
    """验证并解析 JWT。统一转换无效令牌错误。"""

    try:
        payload = jwt.decode(
            token,
            _get_jwt_secret(settings),
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["sub", "username", "role", "iat", "exp"]},
        )

        return AccessTokenClaims(
            user_id=UUID(payload["sub"]),
            username=payload["username"],
            role=UserRole(payload["role"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise InvalidAccessTokenError("Invalid access token") from exc
