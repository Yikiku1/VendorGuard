"""断言 BM25 与向量检索所需的 PostgreSQL 扩展确实已安装.

这是一条回归防护测试, 不承担红绿职责: ParadeDB 镜像在 template1 里预装了 pg_search
与 vector, 所以它在今天本来就是绿的. 它防的是将来换回普通 postgres 镜像或误删扩展时,
问题要拖到第 5 步建向量列或第 7 步建 BM25 索引时才炸开, 而现场看起来像"代码写错了".

迁移里显式 CREATE EXTENSION IF NOT EXISTS 的可复现性不靠本文件证明, 由一次性的
template0 实验证明: 从一个不继承预装扩展的空库跑完整迁移链, 扩展仍须存在.
"""

from sqlalchemy import text

from vendorguard.config import load_settings
from vendorguard.database import create_database_engine

# BM25 词法召回与 pgvector 向量召回各自依赖一个扩展, 缺一即整条混合检索不成立.
REQUIRED_EXTENSIONS = frozenset({"pg_search", "vector"})


async def test_bm25_and_vector_extensions_are_installed() -> None:
    """混合检索依赖的两个扩展必须已安装, 缺失时给出明确断言而不是让建表报错."""

    engine = create_database_engine(load_settings())

    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT extname FROM pg_extension"))
            installed = {str(row[0]) for row in result.fetchall()}
            missing = sorted(REQUIRED_EXTENSIONS - installed)

            assert not missing, f"缺少检索依赖的扩展: {missing}; 已安装: {sorted(installed)}"
    finally:
        await engine.dispose()
