from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.database import Base


class AuditSubjectType(StrEnum):
    ADMISSION_CASE = "admission_case"
    SUPPLIER = "supplier"


class AuditEventType(StrEnum):
    ADMISSION_CASE_CREATED = "admission_case_created"
    DOCUMENTS_REGISTERED = "documents_registered"
    ADMISSION_CASE_STATUS_CHANGED = "admission_case_status_changed"
    ADMISSION_CASE_DECISION_RECORDED = "admission_case_decision_recorded"
    SUPPLIER_ELIGIBILITY_CHANGED = "supplier_eligibility_changed"
    RULES_EVALUATED = "rules_evaluated"
    RULE_HIT_RECORDED = "rule_hit_recorded"


_SUBJECT_VALUES_SQL = ", ".join(f"'{subject.value}'" for subject in AuditSubjectType)
_EVENT_VALUES_SQL = ", ".join(f"'{event.value}'" for event in AuditEventType)


class AuditEvent(Base):
    """保存不可覆盖的业务操作事实。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            f"subject_type IN ({_SUBJECT_VALUES_SQL})",
            name="audit_subject_type",
        ),
        CheckConstraint(
            f"event_type IN ({_EVENT_VALUES_SQL})",
            name="audit_event_type",
        ),
        Index(
            "ix_audit_events_subject",
            "subject_type",
            "subject_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    subject_type: Mapped[AuditSubjectType] = mapped_column(
        Enum(
            AuditSubjectType,
            name="audit_subject_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [subject.value for subject in enum_type],
        ),
        nullable=False,
    )
    subject_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(
            AuditEventType,
            name="audit_event_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [event.value for event in enum_type],
        ),
        nullable=False,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


async def append_audit_event(
    session: AsyncSession,
    *,
    subject_type: AuditSubjectType,
    subject_id: UUID,
    event_type: AuditEventType,
    actor_user_id: UUID | None,
    payload: Mapping[str, object],
) -> AuditEvent:
    event = AuditEvent(
        subject_type=subject_type,
        subject_id=subject_id,
        event_type=event_type,
        actor_user_id=actor_user_id,
        payload=dict(payload),
    )
    session.add(event)
    await session.flush()
    return event


async def list_audit_events(
    session: AsyncSession,
    *,
    subject_type: AuditSubjectType,
    subject_id: UUID,
) -> list[AuditEvent]:
    event = await session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.subject_type == subject_type,
            AuditEvent.subject_id == subject_id,
        )
        .order_by(AuditEvent.id)
    )
    return list(event)
