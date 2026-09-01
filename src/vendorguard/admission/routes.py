from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
