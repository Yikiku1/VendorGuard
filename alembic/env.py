import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from vendorguard.admission import AdmissionCase  # noqa: F401
from vendorguard.alembic_filters import load_extension_owned_tables, make_include_object
from vendorguard.audit import AuditEvent  # noqa: F401
from vendorguard.config import load_settings
from vendorguard.database import Base, build_database_url
from vendorguard.evidence import models  # noqa: F401
from vendorguard.security import User  # noqa: F401
from vendorguard.suppliers import Supplier  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
database_url = build_database_url(load_settings()).render_as_string(hide_password=False)
config.set_main_option(
    "sqlalchemy.url",
    database_url.replace("%", "%%"),
)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """在不创建 Engine 的情况下生成离线迁移 SQL。"""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection, excluded_tables: set[str]) -> None:
    """使用已有同步连接执行迁移, 并把扩展自带对象挡在自动比对之外.

    排除名单由调用方在另一条连接上读好后传进来. 迁移连接一旦被提前查询, SQLAlchemy 会
    autobegin 一个事务, alembic 接管时复用了它并不拥有的事务边界, commit 会被跳过,
    表现就是日志显示 Running upgrade、退出码 0、而 alembic_version 一行都没变.

    include_object 同时作用于 upgrade、downgrade 与 check.
    """

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=make_include_object(excluded_tables),
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建异步 Engine 并执行在线迁移.

    读名单与跑迁移各用一条独立连接: NullPool 下每条连接都是真实的新连接, 多一次连接的
    代价远小于把迁移连接的事务所有权搞混. 读完立刻退出 with 块释放, 保证交给 alembic 的
    那条连接是干净的.
    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as probe:
        excluded_tables = await probe.run_sync(load_extension_owned_tables)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations, excluded_tables)

    await connectable.dispose()


def run_migrations_online() -> None:
    """从同步入口启动异步在线迁移。"""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
