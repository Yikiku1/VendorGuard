from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission import (
    AdmissionCase,
    AdmissionCaseStatus,
    DocumentType,
    create_admission_case,
    get_admission_case_detail,
    register_document_metadata,
    transition_admission_case,
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

    model_config = ConfigDict(from_attributes=True)

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


class RegisterDocumentRequest(BaseModel):
    """登记准入材料元数据的请求体。"""

    document_type: DocumentType
    original_file_name: str = Field(..., min_length=1, max_length=255)
    media_type: str = Field(..., min_length=1, max_length=100)
    file_size_bytes: int = Field(..., gt=0)
    sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )


class DocumentResponse(BaseModel):
    """材料元数据及对应审计事件的响应。"""

    id: UUID
    admission_case_id: UUID
    uploaded_by_user_id: UUID
    document_type: DocumentType
    original_file_name: str
    media_type: str
    file_size_bytes: int
    sha256: str
    created_at: datetime
    audit_event: AuditEventResponse


class DocumentDetailResponse(BaseModel):
    """案件详情中的材料元数据响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    admission_case_id: UUID
    uploaded_by_user_id: UUID
    document_type: DocumentType
    original_file_name: str
    media_type: str
    file_size_bytes: int
    sha256: str
    created_at: datetime


class AdmissionCaseDetailResponse(BaseModel):
    """准入案件详情及其关联对象。"""

    id: UUID
    supplier_id: UUID
    submitted_by_user_id: UUID
    status: AdmissionCaseStatus
    created_at: datetime
    updated_at: datetime
    supplier: SupplierResponse
    documents: list[DocumentDetailResponse]
    audit_events: list[AuditEventResponse]


class TransitionAdmissionCaseRequest(BaseModel):
    """迁移准入案件状态的请求体"""

    target_status: AdmissionCaseStatus


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


@router.post(
    "/cases/{admission_case_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_document_metadata_endpoint(
    admission_case_id: UUID,
    request: RegisterDocumentRequest,
    db: Annotated[AsyncSession, Depends(get_database_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DocumentResponse:
    """采购专员为已有准入案件登记一份材料元数据。"""

    if current_user.role != UserRole.PROCUREMENT_SPECIALIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权限登记准入材料",
        )

    admission_case = await db.get(AdmissionCase, admission_case_id)
    if admission_case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="准入案件不存在",
        )

    try:
        document, audit_event = await register_document_metadata(
            db,
            admission_case_id=admission_case_id,
            uploaded_by_user_id=current_user.id,
            document_type=request.document_type,
            original_file_name=request.original_file_name,
            media_type=request.media_type,
            file_size_bytes=request.file_size_bytes,
            sha256=request.sha256,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_documents_case_sha256" in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该材料已登记",
            ) from exc
        raise

    return DocumentResponse(
        id=document.id,
        admission_case_id=document.admission_case_id,
        uploaded_by_user_id=document.uploaded_by_user_id,
        document_type=document.document_type,
        original_file_name=document.original_file_name,
        media_type=document.media_type,
        file_size_bytes=document.file_size_bytes,
        sha256=document.sha256,
        created_at=document.created_at,
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


@router.get(
    "/cases/{admission_case_id}",
    response_model=AdmissionCaseDetailResponse,
)
async def get_admission_case_detail_endpoint(
    admission_case_id: UUID,
    db: Annotated[AsyncSession, Depends(get_database_session)],
    _: Annotated[User, Depends(get_current_user)],
) -> AdmissionCaseDetailResponse:
    """查询准入案件详情。"""

    detail = await get_admission_case_detail(
        db,
        admission_case_id=admission_case_id,
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="准入案件不存在",
        )

    admission_case, supplier, documents, audit_events = detail

    return AdmissionCaseDetailResponse(
        id=admission_case.id,
        supplier_id=admission_case.supplier_id,
        submitted_by_user_id=admission_case.submitted_by_user_id,
        status=admission_case.status,
        created_at=admission_case.created_at,
        updated_at=admission_case.updated_at,
        supplier=SupplierResponse.model_validate(supplier),
        documents=[DocumentDetailResponse.model_validate(document) for document in documents],
        audit_events=[AuditEventResponse.model_validate(event) for event in audit_events],
    )


@router.post(
    "/cases/{admission_case_id}/transition",
    response_model=AdmissionCaseResponse,
)
async def transition_admission_case_endpoint(
    admission_case_id: UUID,
    request: TransitionAdmissionCaseRequest,
    db: Annotated[AsyncSession, Depends(get_database_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdmissionCaseResponse:
    """采购专员迁移准入案件状态。"""

    if current_user.role != UserRole.PROCUREMENT_SPECIALIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权限迁移准入案件",
        )

    try:
        result = await transition_admission_case(
            db,
            admission_case_id=admission_case_id,
            target_status=request.target_status,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="准入案件不存在",
        )

    admission_case, audit_event = result
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
