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
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

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


async def create_supplier(
    db: AsyncSession,
    *,
    display_name: str,
    declared_registration_id: str,
    category_code: str,
) -> Supplier:
    """创建供应商主体, 默认资格为候选状态"""

    supplier = Supplier(
        display_name=display_name,
        declared_registration_id=declared_registration_id,
        category_code=category_code,
        eligibility_status=SupplierEligibility.CANDIDATE,
    )

    db.add(supplier)
    await db.flush()  # 只刷新, 不提交; 让事务由调用方控制

    return supplier
