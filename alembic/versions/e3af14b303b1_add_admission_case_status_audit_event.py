"""add admission case status audit event

Revision ID: e3af14b303b1
Revises: 9be8c214387a
Create Date: 2026-09-01 21:39:31.048205

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3af14b303b1"
down_revision: str | Sequence[str] | None = "9be8c214387a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许状态变化审计事件并扩大事件类型字段。"""

    op.alter_column(
        "audit_events",
        "event_type",
        existing_type=sa.String(length=22),
        type_=sa.String(length=29),
        existing_nullable=False,
    )

    op.drop_constraint(
        "audit_event_type",
        "audit_events",
        type_="check",
    )
    op.create_check_constraint(
        "audit_event_type",
        "audit_events",
        (
            "event_type IN ("
            "'admission_case_created', "
            "'documents_registered', "
            "'admission_case_status_changed'"
            ")"
        ),
    )


def downgrade() -> None:
    """恢复旧的审计事件约束和字段长度。"""

    op.drop_constraint(
        "audit_event_type",
        "audit_events",
        type_="check",
    )
    op.create_check_constraint(
        "audit_event_type",
        "audit_events",
        ("event_type IN ('admission_case_created', 'documents_registered')"),
    )

    op.alter_column(
        "audit_events",
        "event_type",
        existing_type=sa.String(length=29),
        type_=sa.String(length=22),
        existing_nullable=False,
    )
