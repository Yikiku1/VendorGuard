"""alembic 扩展对象排除规则的单元测试.

本文件守的是质量门禁本身: alembic check 之所以可信, 是因为它只放过被 PostgreSQL
记为某个扩展所有的表, 其余对象一律比对. 测试重点不在于验证扩展表被排除 (那是功能),
而在于验证业务表绝不会被误排除 (那是防吞).
"""

from collections.abc import Callable

from sqlalchemy import Column, Index, Integer, MetaData, Table, Text

from vendorguard.alembic_filters import make_include_object


def _business_metadata() -> MetaData:
    """构造一份只含业务表的干净元数据, 供排除函数比对使用."""

    metadata = MetaData()
    Table(
        "admission_cases",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("note", Text),
    )
    return metadata


def _compare_table(include: Callable[..., bool], table: Table) -> bool:
    """按 Alembic 的回调签名对一张表做一次是否参与比对的判定, 避免断言行超长."""

    return include(table, table.name, "table", True, None)


def test_extension_owned_table_is_excluded() -> None:
    """被列入扩展所有物集合的表应当排除, 避免门禁被镜像自带表搞红."""

    metadata = _business_metadata()
    extension_table = Table("spatial_ref_sys", metadata, Column("srid", Integer, primary_key=True))
    include = make_include_object({"spatial_ref_sys"})

    assert _compare_table(include, extension_table) is False


def test_business_table_is_still_compared() -> None:
    """防吞主测试: 业务表即使与扩展表同处 public schema, 也必须照常比对."""

    metadata = _business_metadata()
    business_table = metadata.tables["admission_cases"]
    include = make_include_object({"spatial_ref_sys", "_typmod_cache"})

    assert _compare_table(include, business_table) is True


def test_extension_table_index_is_excluded() -> None:
    """扩展表的主键索引若不排除, 门禁仍会报出残留对象, 排除要能沿父子关系传递."""

    metadata = _business_metadata()
    extension_table = Table("spatial_ref_sys", metadata, Column("srid", Integer, primary_key=True))
    extension_index = Index("spatial_ref_sys_pkey", extension_table.c.srid)
    include = make_include_object({"spatial_ref_sys"})

    assert include(extension_index, "spatial_ref_sys_pkey", "index", True, None) is False


def test_business_table_index_is_not_excluded() -> None:
    """防吞第二道: 业务表上的索引必须比对, 否则真实的索引缺失会永久隐身."""

    metadata = _business_metadata()
    business_index = Index("ix_admission_cases_note", metadata.tables["admission_cases"].c.note)
    include = make_include_object({"spatial_ref_sys", "_typmod_cache"})

    assert include(business_index, "ix_admission_cases_note", "index", True, None) is True


def test_empty_exclusion_set_compares_everything() -> None:
    """查不到扩展所有物时规则必须退化为不排除任何东西, 宁可门禁变红也不能静默放行."""

    metadata = _business_metadata()
    extension_table = Table("spatial_ref_sys", metadata, Column("srid", Integer, primary_key=True))
    include = make_include_object(set())

    assert _compare_table(include, metadata.tables["admission_cases"]) is True
    assert _compare_table(include, extension_table) is True


def test_non_table_objects_are_never_excluded() -> None:
    """排除规则的权力范围仅限表及其附属对象, 不得顺手扩到 schema 或自定义类型.

    这里故意传 obj=None: schema 与自定义类型没有母表可比, 规则只要不越权就应放行,
    因此本用例不需要真实元数据.
    """

    include = make_include_object({"spatial_ref_sys"})

    assert include(None, "public", "schema", True, None) is True
    assert include(None, "user_role", "type", True, None) is True
