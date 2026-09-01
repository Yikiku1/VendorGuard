from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission import (
    AdmissionCaseStatus,
    create_admission_case,
)
from vendorguard.audit import AuditEventType, AuditSubjectType
from vendorguard.database import get_database_session
from vendorguard.dependencies import get_current_user
from vendorguard.security import User, UserRole
from vendorguard.suppliers import (
    Supplier,
    SupplierEligibility,
    create_supplier,
)

router = APIRouter(prefix="/api/admission", tags=["admission"])


# ==================== Pydantic 模型 ====================


class CreateSupplierRequest(BaseModel):
    """创建供应商的请求体"""

    display_name: str = Field(..., min_length=1, max_length=200)
    declared_registration_id: str = Field(..., min_length=1, max_length=100)
    category_code: str = Field(..., min_length=1, max_length=64)


class SupplierResponse(BaseModel):
    """供应商响应"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    declared_registration_id: str
    category_code: str
    eligibility_status: SupplierEligibility
    created_at: datetime


class CreateAdmissionCaseRequest(BaseModel):
    """创建准入案件的请求体"""

    supplier_id: UUID


class AuditEventResponse(BaseModel):
    """创建案件返回时的审计事件"""

    id: int
    subject_type: AuditSubjectType
    subject_id: UUID
    event_type: AuditEventType
    actor_user_id: UUID | None
    payload: dict[str, object]
    occurred_at: datetime


class AdmissionCaseResponse(BaseModel):
    """新建准入案件及其首条审计事件"""

    id: UUID
    supplier_id: UUID
    submitted_by_user_id: UUID
    status: AdmissionCaseStatus
    created_at: datetime
    audit_event: AuditEventResponse


# ==================== HTTP 路由 ====================


@router.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier_endpoint(
    request: CreateSupplierRequest,
    db: Annotated[AsyncSession, Depends(get_database_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Supplier:
    """采购专员创建供应商主体。"""

    # 只有采购专员可以创建供应商。
    if current_user.role != UserRole.PROCUREMENT_SPECIALIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权限创建供应商",
        )

    try:
        supplier = await create_supplier(
            db=db,
            display_name=request.display_name,
            declared_registration_id=request.declared_registration_id,
            category_code=request.category_code,
        )
        await db.commit()
        return supplier

    except IntegrityError as e:
        await db.rollback()
        # 统一社会信用代码重复
        if "declared_registration_id" in str(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该统一社会信用代码已存在",
            ) from e
        raise


@router.post(
    "/cases",
    response_model=AdmissionCaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_admission_case_endpoint(
    request: CreateAdmissionCaseRequest,
    db: Annotated[AsyncSession, Depends(get_database_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdmissionCaseResponse:
    """采购专员基于已有供应商创建草稿准入案件"""

    if current_user.role != UserRole.PROCUREMENT_SPECIALIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权限创建准入案件",
        )

    supplier = await db.get(Supplier, request.supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="供应商不存在",
        )

    admission_case, audit_event = await create_admission_case(
        db, supplier_id=request.supplier_id, submitted_by_user_id=current_user.id
    )
    await db.commit()

    return AdmissionCaseResponse(
        id=admission_case.id,
        supplier_id=admission_case.supplier_id,
        submitted_by_user_id=admission_case.submitted_by_user_id,
        status=admission_case.status,
        created_at=admission_case.created_at,
        audit_event=AuditEventResponse(
            id=audit_event.id,
            subject_type=audit_event.subject_type,
            subject_id=audit_event.subject_id,
            event_type=audit_event.event_type,
            actor_user_id=audit_event.actor_user_id,
            payload=audit_event.payload,
            occurred_at=audit_event.occurred_at,
        ),
    )
