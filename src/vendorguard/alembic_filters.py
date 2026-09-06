"""为 alembic 自动比对提供扩展对象排除规则.

数据库发行版镜像会在 template1 里预装扩展, 新建库因此自动继承 postgis 的
spatial_ref_sys 或 pg_search 的 _typmod_cache 这类扩展自带表. 它们不在项目模型中,
alembic check 就会持续报 remove_table 漂移.

排除依据取 PostgreSQL 自己记录的归属关系而不是硬编码表名: 镜像将来多装一个扩展,
它的表自动被排除, 门禁不必再改代码. 规则采用默认放行、例外排除, 保证业务表的
漂移永远不会被顺带吞掉.
"""

from collections.abc import Callable, Collection
from typing import Any

from sqlalchemy import Connection, text

_EXTENSION_OWNED_TABLES_SQL = """
SELECT DISTINCT c.relname
FROM pg_depend AS d
JOIN pg_class AS c ON c.oid = d.objid
JOIN pg_extension AS e ON e.oid = d.refobjid
WHERE d.classid = 'pg_class'::regclass
  AND d.refclassid = 'pg_extension'::regclass
  AND d.deptype = 'e'
  AND c.relkind IN ('r', 'p')
"""

# Alembic 会为表的附属对象单独回调一次, 这些对象要跟着母表一起决定去留.
_CHILD_OBJECT_TYPES = frozenset(
    {"index", "constraint", "unique_constraint", "foreign_key_constraint"}
)


def load_extension_owned_tables(connection: Connection) -> set[str]:
    """查出当前库中所有归属于某个扩展的表名.

    只认 deptype = 'e' 这条官方归属语义, 不用"表名长得像扩展表"这类猜测. 接收同步
    Connection 是为了让 alembic 的 run_sync 与异步集成测试复用同一条查询路径.
    """

    rows = connection.execute(text(_EXTENSION_OWNED_TABLES_SQL)).scalars().all()

    return {str(row) for row in rows}


def make_include_object(excluded_tables: Collection[str]) -> Callable[..., bool]:
    """按扩展所有物名单生成 alembic 的 include_object 回调."""

    excluded = frozenset(excluded_tables)

    def include_object(obj: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
        """默认放行, 只把扩展表及其附属对象挡在比对之外."""

        if type_ == "table" and name in excluded:
            return False

        parent_table = getattr(obj, "table", None)
        if type_ in _CHILD_OBJECT_TYPES and parent_table is not None:
            return getattr(parent_table, "name", None) not in excluded

        return True

    return include_object
