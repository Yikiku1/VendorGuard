"""enable hybrid search extensions

Revision ID: 8acab623700e
Revises: 0a22cbb97473
Create Date: 2026-09-06 13:17:25.396192

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8acab623700e"
down_revision: Union[str, Sequence[str], None] = "0a22cbb97473"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """显式启用混合检索依赖的两个扩展, 顺序必须是先 vector 后 pg_search.

    ParadeDB 镜像在 template1 里预装了它们, 所以常规新库里两条都是空操作. 但在从 template0
    建的干净库上跑完整迁移链时, 先建 pg_search 会直接报 required extension "vector" is not
    installed: pg_search 的扩展元数据声明了对 vector 的硬依赖. 镜像预装会掩盖这个顺序问题,
    只有干净库能暴露, 所以这里记的是实测结论而不是排版偏好.
    """

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")


def downgrade() -> None:
    """回退时只撤销 schema, 不卸载扩展.

    扩展是数据库级共享资源: DROP EXTENSION pg_search 会连带删掉它拥有的索引与缓存表,
    而在同一库里的其他对象也可能依赖它. 撤销本项目 schema 变更不应产生卸载数据库组件这种
    超出迁移职责的副作用, 因此这里刻意留空.
    """
