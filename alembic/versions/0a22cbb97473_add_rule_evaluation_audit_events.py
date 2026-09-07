"""add rule evaluation audit events

Revision ID: 0a22cbb97473
Revises: 1fe47cf7cb3e
Create Date: 2026-09-05 18:32:52.423748

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0a22cbb97473"
down_revision: Union[str, Sequence[str], None] = "1fe47cf7cb3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_EVENTS = (
    "admission_case_created",
    "documents_registered",
    "admission_case_status_changed",
    "admission_case_decision_recorded",
    "supplier_eligibility_changed",
    "rules_evaluated",
    "rule_hit_recorded",
)
_OLD_EVENTS = _NEW_EVENTS[:5]


def _event_check(values: tuple[str, ...]) -> str:
    """拼 event_type IN (...) 的 CHECK 表达式。"""

    return "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    """为审计事件 CHECK 约束追加规则评估两类新事件, 列宽不变故无需 alter_column。"""

    op.drop_constraint("audit_event_type", "audit_events", type_="check")
    op.create_check_constraint("audit_event_type", "audit_events", _event_check(_NEW_EVENTS))


def downgrade() -> None:
    """回退到仅五类事件的旧 CHECK 约束。"""

    op.drop_constraint("audit_event_type", "audit_events", type_="check")
    op.create_check_constraint("audit_event_type", "audit_events", _event_check(_OLD_EVENTS))
