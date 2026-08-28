from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    String,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.database import Base


class UserRole(StrEnum):
    PROCUREMENT_SPECIALIST = "procurement_specialist"
    PROCUREMENT_MANAGER = "procurement_manager"
    QUALITY_MANAGER = "quality_manager"
    ADMIN = "admin"


_USER_ROLE_VALUES_SQL = ", ".join(
    f"'{role.value}'" for role in UserRole
)


class User(Base):
    __tablename__ = "users"
    
    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [
                role.value for role in enum_type
            ],
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )