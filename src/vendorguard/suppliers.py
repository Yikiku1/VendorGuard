from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.database import Base


class SupplierEligibility(StrEnum):
    """供应商当前是否具备常规采购资格"""

    CANDIDATE = "candidate"
    APPROVED = "approved"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    REJECTED = "rejected"


_ELIGIBILITY_VALUES_SQL = ", ".join(f"'{status.value}'" for status in SupplierEligibility)


class Supplier(Base):
    """保存供应商主体及其当前准入资格"""

    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint(
            f"eligibility_status IN ({_ELIGIBILITY_VALUES_SQL})", name="supplier_eligibility_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    declared_registration_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )
    category_code: Mapped[str] = mapped_column(String(64), nullable=False)
    eligibility_status: Mapped[SupplierEligibility] = mapped_column(
        Enum(
            SupplierEligibility,
            name="supplier_eligibility_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [status.value for status in enum_type],
        ),
        default=SupplierEligibility.CANDIDATE,
        server_default=SupplierEligibility.CANDIDATE.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
