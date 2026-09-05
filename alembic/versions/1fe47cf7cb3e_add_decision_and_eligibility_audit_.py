"""add decision and eligibility audit events

Revision ID: 1fe47cf7cb3e
Revises: e3af14b303b1
Create Date: <保留你原来的时间戳, 不要动>

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1fe47cf7cb3e"
down_revision: str | Sequence[str] | None = "e3af14b303b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许审批决定与供应商资格变化两类新审计事件, 并支持 supplier 作为审计主体。"""

    # 事件枚举扩到 5 值, 最长 admission_case_decision_recorded 31 字符, 需要扩物理宽度。
    # 用 sa.Enum 而不是 sa.String, 与 SQLAlchemy 模型定义一致, 避免 autogenerate 误报。
    op.alter_column(
        "audit_events",
        "event_type",
        existing_type=sa.Enum(
            "admission_case_created",
            "documents_registered",
            "admission_case_status_changed",
            name="audit_event_type",
            native_enum=False,
            create_constraint=False,
        ),
        type_=sa.Enum(
            "admission_case_created",
            "documents_registered",
            "admission_case_status_changed",
            "admission_case_decision_recorded",
            "supplier_eligibility_changed",
            name="audit_event_type",
            native_enum=False,
            create_constraint=False,
        ),
        existing_nullable=False,
    )

    # 更新 event_type CHECK 约束, 加入两个新事件类型。
    op.drop_constraint("audit_event_type", "audit_events", type_="check")
    op.create_check_constraint(
        "audit_event_type",
        "audit_events",
        (
            "event_type IN ("
            "'admission_case_created', "
            "'documents_registered', "
            "'admission_case_status_changed', "
            "'admission_case_decision_recorded', "
            "'supplier_eligibility_changed'"
            ")"
        ),
    )

    # 更新 subject_type CHECK 约束, 加入 supplier 作为新的审计主体。
    op.drop_constraint("audit_subject_type", "audit_events", type_="check")
    op.create_check_constraint(
        "audit_subject_type",
        "audit_events",
        ("subject_type IN ('admission_case', 'supplier')"),
    )


def downgrade() -> None:
    """回退到只有 admission_case 主体和三种事件的旧约束。"""

    op.drop_constraint("audit_subject_type", "audit_events", type_="check")
    op.create_check_constraint(
        "audit_subject_type",
        "audit_events",
        ("subject_type IN ('admission_case')"),
    )

    op.drop_constraint("audit_event_type", "audit_events", type_="check")
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

    op.alter_column(
        "audit_events",
        "event_type",
        existing_type=sa.Enum(
            "admission_case_created",
            "documents_registered",
            "admission_case_status_changed",
            "admission_case_decision_recorded",
            "supplier_eligibility_changed",
            name="audit_event_type",
            native_enum=False,
            create_constraint=False,
        ),
        type_=sa.Enum(
            "admission_case_created",
            "documents_registered",
            "admission_case_status_changed",
            name="audit_event_type",
            native_enum=False,
            create_constraint=False,
        ),
        existing_nullable=False,
    )