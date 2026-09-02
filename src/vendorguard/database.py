from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from vendorguard.config import Settings


class Base(DeclarativeBase):
    """为所有 SQLAlchemy ORM 模型提供统一声明基类。"""

    pass


def build_database_url(settings: Settings) -> URL:
    """根据应用配置构造不会意外展示密码的数据库 URL。"""

    if settings.db_password is None:
        raise ValueError("必须配置数据库密码")

    return URL.create(
        drivername="postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def create_database_engine(settings: Settings) -> AsyncEngine:
    """创建启用连接健康检查的异步数据库 Engine。"""

    return create_async_engine(build_database_url(settings), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """为指定 Engine 创建异步 Session 工厂。"""

    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_database_session(
    request: Request,
) -> AsyncIterator[AsyncSession]:
    """为单个 HTTP 请求提供并自动关闭数据库 Session。"""

    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory

    async with session_factory() as session:
        yield session


@asynccontextmanager
async def transactional_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """提供成功提交、异常回滚的事务型 Session 上下文。"""

    async with session_factory() as session, session.begin():
        yield session
