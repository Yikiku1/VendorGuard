"""add edition required conditions

Revision ID: c4e7a4d2b809
Revises: 2f31cceb123a
Create Date: 2026-09-07 14:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4e7a4d2b809"
down_revision: str | Sequence[str] | None = "2f31cceb123a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """将条件性来源的前提转为可由查询执行的版本元数据。"""

    op.add_column(
        "knowledge_editions",
        sa.Column(
            "required_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "knowledge_edition_required_conditions_array",
        "knowledge_editions",
        "jsonb_typeof(required_conditions) = 'array'",
    )


def downgrade() -> None:
    """移除条件性来源前提字段。"""

    op.drop_constraint(
        "knowledge_edition_required_conditions_array",
        "knowledge_editions",
        type_="check",
    )
    op.drop_column("knowledge_editions", "required_conditions")
