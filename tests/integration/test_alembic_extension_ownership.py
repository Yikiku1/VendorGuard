"""扩展所有物查询在真实数据库上的集成测试.

单元测试只能证明给定名单时排除逻辑不误伤业务表; 本文件证明前提本身:
从 pg_depend 查出的扩展所有物名单, 在真实库里绝不包含我们自己声明的任何表.
这条不相交断言是结构性保证, 只要它绿, env.py 的排除规则就不可能吞掉真实漂移.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vendorguard.alembic_filters import load_extension_owned_tables
from vendorguard.config import load_settings
from vendorguard.database import Base, create_database_engine


async def _installed_extensions(connection: AsyncConnection) -> set[str]:
    """返回当前库已安装的扩展名, 用于判断镜像差异是否让某条断言失去意义."""

    result = await connection.execute(text("SELECT extname FROM pg_extension"))

    return {str(row[0]) for row in result.fetchall()}


async def test_extension_owned_tables_never_contain_model_tables() -> None:
    """pg_depend 认定的扩展表不得包含任何模型表, 这是排除规则不误伤的硬保证."""

    engine = create_database_engine(load_settings())

    try:
        async with engine.connect() as connection:
            owned = await connection.run_sync(load_extension_owned_tables)
            model_tables = set(Base.metadata.tables)
            overlap = sorted(owned & model_tables)

            assert not overlap, f"业务表被识别为扩展所有物, 门禁会吞掉它们的漂移: {overlap}"
    finally:
        await engine.dispose()


async def test_extension_owned_tables_returns_name_set() -> None:
    """env.py 直接用这个集合按名字比对, 混入空串或非字符串会让排除静默失效."""

    engine = create_database_engine(load_settings())

    try:
        async with engine.connect() as connection:
            owned = await connection.run_sync(load_extension_owned_tables)

            assert isinstance(owned, set)
            assert all(isinstance(name, str) and name for name in owned)
    finally:
        await engine.dispose()


async def test_image_bundled_extension_tables_are_detected() -> None:
    """ParadeDB 镜像预装 postgis 与 pg_search, 它们带进来的表必须被名单捕获."""

    engine = create_database_engine(load_settings())

    try:
        async with engine.connect() as connection:
            installed = await _installed_extensions(connection)
            owned = await connection.run_sync(load_extension_owned_tables)

            if "postgis" not in installed:
                # 换回不含 postgis 的镜像时这条断言失去对象, 选择跳过而不是删掉,
                # 保留它才能在真镜像上继续守住 spatial_ref_sys 这类自带表.
                pytest.skip("当前数据库镜像未安装 postgis, 该断言不适用")

            assert "spatial_ref_sys" in owned
    finally:
        await engine.dispose()
