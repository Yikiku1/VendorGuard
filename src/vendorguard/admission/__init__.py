from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.audit import (
    AuditEvent,
    AuditEventType,
    AuditSubjectType,
    append_audit_event,
    list_audit_events,
)
from vendorguard.database import Base
from vendorguard.suppliers import Supplier, SupplierEligibility


class AdmissionCaseStatus(StrEnum):
    """一次供应商准入申请的业务进度"""

    DRAFT = "draft"
    PENDING_DOCUMENTS = "pending_documents"
    ANALYZING = "analyzing"
    EVIDENCE_REVIEWING = "evidence_reviewing"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class AdmissionDecision(StrEnum):
    """采购经理在 pending_approval 阶段可以做出的三种审批决定"""

    APPROVED = "approved"
    REJECTED = "rejected"
    SUPPLEMENT_REQUESTED = "supplement_requested"


_DECISION_TO_TARGET_STATUS: dict[AdmissionDecision, AdmissionCaseStatus] = {
    AdmissionDecision.APPROVED: AdmissionCaseStatus.APPROVED,
    AdmissionDecision.REJECTED: AdmissionCaseStatus.REJECTED,
    AdmissionDecision.SUPPLEMENT_REQUESTED: AdmissionCaseStatus.PENDING_DOCUMENTS,
}


_ALLOWED_ADMISSION_CASE_TRANSITIONS: dict[
    AdmissionCaseStatus,
    frozenset[AdmissionCaseStatus],
] = {
    AdmissionCaseStatus.DRAFT: frozenset({AdmissionCaseStatus.PENDING_DOCUMENTS}),
    AdmissionCaseStatus.PENDING_APPROVAL: frozenset(
        {
            AdmissionCaseStatus.APPROVED,
            AdmissionCaseStatus.REJECTED,
            AdmissionCaseStatus.PENDING_DOCUMENTS,
        }
    ),
}


_CASE_STATUS_VALUES_SQL = ", ".join(f"'{status.value}'" for status in AdmissionCaseStatus)


class AdmissionCase(Base):
    """保存一次供应商准入申请及其当前流程状态。"""

    __tablename__ = "admission_cases"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_CASE_STATUS_VALUES_SQL})",
            name="admission_case_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    status: Mapped[AdmissionCaseStatus] = mapped_column(
        Enum(
            AdmissionCaseStatus,
            name="admission_case_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [status.value for status in enum_type],
        ),
        default=AdmissionCaseStatus.DRAFT,
        server_default=AdmissionCaseStatus.DRAFT.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DocumentType(StrEnum):
    """准入案件支持的业务材料类型。"""

    BUSINESS_LICENSE = "business_license"
    QUALITY_CERTIFICATE = "quality_certificate"
    QUOTATION = "quotation"
    DELIVERY_HISTORY = "delivery_history"


_DOCUMENT_TYPE_VALUES_SQL = ", ".join(f"'{document_type.value}'" for document_type in DocumentType)


class Document(Base):
    """保存准入材料的文件元数据, 不保存文件正文。"""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            f"document_type IN ({_DOCUMENT_TYPE_VALUES_SQL})",
            name="document_type",
        ),
        CheckConstraint(
            "file_size_bytes > 0",
            name="document_file_size_positive",
        ),
        CheckConstraint(
            "char_length(sha256) = 64",
            name="document_sha256_length",
        ),
        UniqueConstraint(
            "admission_case_id",
            "sha256",
            name="uq_documents_case_sha256",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    admission_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("admission_cases.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    uploaded_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_type: Mapped[DocumentType] = mapped_column(
        Enum(
            DocumentType,
            name="document_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [document_type.value for document_type in enum_type],
        ),
        nullable=False,
    )
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


async def create_admission_case(
    session: AsyncSession,
    *,
    supplier_id: UUID,
    submitted_by_user_id: UUID,
) -> tuple[AdmissionCase, AuditEvent]:
    """创建草稿准入案件并追加对应审计事件。"""

    admission_case = AdmissionCase(
        supplier_id=supplier_id,
        submitted_by_user_id=submitted_by_user_id,
    )
    session.add(admission_case)
    await session.flush()

    audit_event = await append_audit_event(
        session,
        subject_type=AuditSubjectType.ADMISSION_CASE,
        subject_id=admission_case.id,
        event_type=AuditEventType.ADMISSION_CASE_CREATED,
        actor_user_id=submitted_by_user_id,
        payload={"supplier_id": str(supplier_id)},
    )

    return admission_case, audit_event


async def register_document_metadata(
    session: AsyncSession,
    *,
    admission_case_id: UUID,
    uploaded_by_user_id: UUID,
    document_type: DocumentType,
    original_file_name: str,
    media_type: str,
    file_size_bytes: int,
    sha256: str,
) -> tuple[Document, AuditEvent]:
    """登记一份材料元数据并追加案件审计事件。"""

    document = Document(
        admission_case_id=admission_case_id,
        uploaded_by_user_id=uploaded_by_user_id,
        document_type=document_type,
        original_file_name=original_file_name,
        media_type=media_type,
        file_size_bytes=file_size_bytes,
        sha256=sha256.lower(),
    )
    session.add(document)
    await session.flush()

    audit_event = await append_audit_event(
        session,
        subject_type=AuditSubjectType.ADMISSION_CASE,
        subject_id=admission_case_id,
        event_type=AuditEventType.DOCUMENTS_REGISTERED,
        actor_user_id=uploaded_by_user_id,
        payload={
            "document_count": 1,
            "document_ids": [str(document.id)],
        },
    )

    return document, audit_event


async def get_admission_case_detail(
    session: AsyncSession,
    *,
    admission_case_id: UUID,
) -> (
    tuple[
        AdmissionCase,
        Supplier,
        list[Document],
        list[AuditEvent],
    ]
    | None
):
    """查询案件、供应商、材料和追加式审计时间线。"""

    admission_case = await session.get(AdmissionCase, admission_case_id)
    if admission_case is None:
        return None

    supplier = await session.get(Supplier, admission_case.supplier_id)
    if supplier is None:
        return None

    documents_result = await session.scalars(
        select(Document)
        .where(Document.admission_case_id == admission_case_id)
        .order_by(Document.created_at, Document.id)
    )
    documents = list(documents_result)

    audit_events = await list_audit_events(
        session,
        subject_type=AuditSubjectType.ADMISSION_CASE,
        subject_id=admission_case_id,
    )

    return admission_case, supplier, documents, audit_events


async def transition_admission_case(
    session: AsyncSession,
    *,
    admission_case_id: UUID,
    target_status: AdmissionCaseStatus,
    actor_user_id: UUID,
) -> tuple[AdmissionCase, AuditEvent] | None:
    """迁移准入案件状态并追加状态变化审计事件。"""

    admission_case = await session.get(AdmissionCase, admission_case_id)
    if admission_case is None:
        return None

    allowed_targets = _ALLOWED_ADMISSION_CASE_TRANSITIONS.get(
        admission_case.status,
        frozenset(),
    )
    if target_status not in allowed_targets:
        raise ValueError(
            f"不支持的准入案件状态迁移: {admission_case.status.value} -> {target_status.value}"
        )

    from_status = admission_case.status
    admission_case.status = target_status
    await session.flush()

    audit_event = await append_audit_event(
        session,
        subject_type=AuditSubjectType.ADMISSION_CASE,
        subject_id=admission_case.id,
        event_type=AuditEventType.ADMISSION_CASE_STATUS_CHANGED,
        actor_user_id=actor_user_id,
        payload={
            "from_status": from_status.value,
            "to_status": target_status.value,
        },
    )

    return admission_case, audit_event


async def record_admission_decision(
    session: AsyncSession,
    *,
    admission_case_id: UUID,
    decision: AdmissionDecision,
    reason: str,
    actor_user_id: UUID,
) -> tuple[AdmissionCase, Supplier | None, list[AuditEvent]] | None:
    """记录审批决定并返回 (案件, 受影响供应商或 None, 本次追加的审计事件序列)。

    approved 与 rejected 会同步更新供应商资格 (candidate → approved/rejected),
    supplement_requested 只回退案件到 pending_documents 且不改供应商资格, 此时
    第二个返回值为 None。审计事件顺序固定为 decision_recorded → status_changed
    → (eligibility_changed?)。

    当前角色模型下 submitter 隔离在权限层天然成立: 采购专员与采购经理是两个独立
    枚举值, 采购经理无法调用创建案件端点, 因此 case.submitted_by_user_id 必不等于
    actor_user_id, 无需额外断言。未来引入多角色用户时须恢复本函数内的显式隔离检查
    与对应测试。
    """

    admission_case = await session.get(AdmissionCase, admission_case_id)
    if admission_case is None:
        return None
    target_status = _DECISION_TO_TARGET_STATUS[decision]
    allowed_targets = _ALLOWED_ADMISSION_CASE_TRANSITIONS.get(
        admission_case.status,
        frozenset(),
    )
    if target_status not in allowed_targets:
        raise ValueError(
            f"不支持的审批决定: {admission_case.status.value} -> {target_status.value}"
        )

    from_status = admission_case.status
    admission_case.status = target_status

    supplier: Supplier | None = None
    supplier_from_eligibility: SupplierEligibility | None = None
    if decision in (AdmissionDecision.APPROVED, AdmissionDecision.REJECTED):
        supplier = await session.get(Supplier, admission_case.supplier_id)
        # 案件存在时其 supplier 必定存在 (FK ondelete=RESTRICT), 断言只用于让 mypy 通过。
        assert supplier is not None
        supplier_from_eligibility = supplier.eligibility_status
        supplier.eligibility_status = (
            SupplierEligibility.APPROVED
            if decision == AdmissionDecision.APPROVED
            else SupplierEligibility.REJECTED
        )

    await session.flush()

    audit_events: list[AuditEvent] = [
        await append_audit_event(
            session,
            subject_type=AuditSubjectType.ADMISSION_CASE,
            subject_id=admission_case.id,
            event_type=AuditEventType.ADMISSION_CASE_DECISION_RECORDED,
            actor_user_id=actor_user_id,
            payload={"decision": decision.value, "reason": reason},
        ),
        await append_audit_event(
            session,
            subject_type=AuditSubjectType.ADMISSION_CASE,
            subject_id=admission_case.id,
            event_type=AuditEventType.ADMISSION_CASE_STATUS_CHANGED,
            actor_user_id=actor_user_id,
            payload={
                "from_status": from_status.value,
                "to_status": target_status.value,
            },
        ),
    ]

    if supplier is not None and supplier_from_eligibility is not None:
        audit_events.append(
            await append_audit_event(
                session,
                subject_type=AuditSubjectType.SUPPLIER,
                subject_id=supplier.id,
                event_type=AuditEventType.SUPPLIER_ELIGIBILITY_CHANGED,
                actor_user_id=actor_user_id,
                payload={
                    "from_eligibility": supplier_from_eligibility.value,
                    "to_eligibility": supplier.eligibility_status.value,
                    "reason": reason,
                },
            )
        )

    return admission_case, supplier, audit_events
