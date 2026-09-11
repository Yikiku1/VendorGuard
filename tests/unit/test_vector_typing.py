"""钉住 pgvector 的 SQLAlchemy 类型可用性与 DDL 渲染.

本文件是向量落库的地基测试: 未安装 pgvector 包时, 连 import 都会失败.
它守的不是业务功能, 而是向量列必须渲染成带显式维度的
vector(n) DDL. 维度一旦缺省或与配置不一致, pgvector 要么拒绝建列, 要么建出一
个与写入向量维度不匹配的列, 后者要到入库时才炸且报错离根因很远。
"""

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, MetaData, Table, create_engine
from sqlalchemy.schema import CreateTable


def _compile_table_ddl(dimensions: int) -> str:
    """生成含向量列的建表 DDL, 检查实际下发给 PostgreSQL 的类型写法.

    返回值统一转小写再断言: pgvector 渲染大写还是小写属于库的排版细节, 断言大小写
    会让测试在库升级时无谓变红.

    方言对象从 create_engine 取而不是直接 postgresql.dialect(): 后者的构造函数没有类型
    标注, 在 mypy strict 下会报 no-untyped-call, 而本项目的原则是不为第三方库的标注缺口
    开 type: ignore. create_engine 只解析 URL 与方言, 不会建立连接, 因此仍是纯单元测试.
    """

    metadata = MetaData()
    table = Table(
        "rag_chunks",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("embedding", Vector(dimensions)),
    )
    engine = create_engine("postgresql+asyncpg://")

    try:
        return str(CreateTable(table).compile(dialect=engine.dialect)).lower()
    finally:
        engine.dispose()


def test_vector_column_renders_explicit_dimension() -> None:
    """向量列必须渲染出带维度的 vector(n), pgvector 不接受无界向量列."""

    assert "vector(1024)" in _compile_table_ddl(1024)


def test_dimension_is_passed_per_column() -> None:
    """维度是按列传入的参数, 512 与 1024 各自渲染, 防止把默认值误当成配置."""

    ddl_512 = _compile_table_ddl(512)

    assert "vector(512)" in ddl_512
    assert "vector(1024)" not in ddl_512
