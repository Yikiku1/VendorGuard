from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.config import load_settings
from vendorguard.database import get_database_session
from vendorguard.security import InvalidAccessTokenError, User, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    session: Annotated[
        AsyncSession,
        Depends(get_database_session),
    ],
) -> User:
    """根据 Bearer Token 查询当前有效用户。"""

    settings = load_settings()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(credentials.credentials, settings)
    except InvalidAccessTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await session.scalar(select(User).where(User.id == claims.user_id))

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user
