"""断言正式库所在的迁移版本就是迁移目录的 head.

这条测试存在的直接起因是 Day 6 第 1 步的一次故障: alembic upgrade head 打印了
Running upgrade 并以退出码 0 结束, 但 alembic_version 里的值纹丝不动. 成功日志与
退出码都无法发现这种故障, 因为 Alembic 只管执行迁移体, 不校验事务最终有没有提交.
把"库必须处于 head"固化成断言之后, 这类静默失败会在下一次跑测试时立刻变红.

顺带守住迁移链不分叉: 出现多个 head 时 upgrade head 的含义本身就不确定.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from vendorguard.config import load_settings
from vendorguard.database import create_database_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _head_revisions() -> list[str]:
    """从迁移目录读出全部 head 修订号, 不连数据库.

    ScriptDirectory 只解析 alembic/versions 下的脚本, 因此这段是纯本地检查,
    与库里实际版本无关 —— 正是这种独立性才能暴露"跑了但没提交".
    """

    config = Config(str(REPO_ROOT / "alembic.ini"))

    return list(ScriptDirectory.from_config(config).get_heads())


async def test_database_is_at_head_revision() -> None:
    """库里的 version_num 必须等于迁移目录的唯一 head."""

    heads = _head_revisions()
    assert len(heads) == 1, f"迁移链出现分叉, heads={heads}"
    expected_head = heads[0]

    engine = create_database_engine(load_settings())

    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = str(result.scalar_one())
    finally:
        await engine.dispose()

    assert current == expected_head, (
        f"库停留在 {current}, 迁移 head 是 {expected_head}. "
        "先跑 uv run alembic upgrade head, 再直接读 alembic_version 那一行确认它真的变了; "
        "日志显示 Running upgrade 并不代表事务已提交."
    )
