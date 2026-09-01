from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission import AdmissionCase, Document, DocumentType
from vendorguard.app import create_app
from vendorguard.config import load_settings
from vendorguard.database import (
    create_database_engine,
    create_session_factory,
    get_database_session,
)
from vendorguard.security import User
from vendorguard.suppliers import Supplier


@pytest_asyncio.fixture
async def existing_supplier_id() -> AsyncIterator[UUID]:
    """创建并清理案件测试需要的已有候选供应商。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            supplier = Supplier(
                display_name="准入案件测试供应商",
                declared_registration_id=f"CASE-{uuid4()}",
                category_code="electronic_components",
            )
            session.add(supplier)
            await session.flush()
            supplier_id = supplier.id

        yield supplier_id
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(delete(Supplier).where(Supplier.id == supplier_id))
        await engine.dispose()


@pytest_asyncio.fixture
async def existing_admission_case_id(
    existing_supplier_id: UUID,
) -> AsyncIterator[UUID]:
    """创建并清理材料登记测试需要的已有案件。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            specialist = await session.scalar(
                select(User).where(User.username == "demo.specialist")
            )
            assert specialist is not None

            admission_case = AdmissionCase(
                supplier_id=existing_supplier_id,
                submitted_by_user_id=specialist.id,
            )
            session.add(admission_case)
            await session.flush()
            admission_case_id = admission_case.id

        yield admission_case_id
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(AdmissionCase).where(AdmissionCase.id == admission_case_id)
            )
        await engine.dispose()


@pytest_asyncio.fixture
async def existing_document_sha256(
    existing_admission_case_id: UUID,
) -> AsyncIterator[str]:
    """为重复材料测试预置一份已经登记的材料。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)
    document_sha256 = "e" * 64

    try:
        async with session_factory() as session, session.begin():
            specialist = await session.scalar(
                select(User).where(User.username == "demo.specialist")
            )
            assert specialist is not None

            document = Document(
                admission_case_id=existing_admission_case_id,
                uploaded_by_user_id=specialist.id,
                document_type=DocumentType.BUSINESS_LICENSE,
                original_file_name="existing-license.pdf",
                media_type="application/pdf",
                file_size_bytes=1024,
                sha256=document_sha256,
            )
            session.add(document)
            await session.flush()

        yield document_sha256
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(Document).where(
                    Document.admission_case_id == existing_admission_case_id,
                    Document.sha256 == document_sha256,
                )
            )
        await engine.dispose()


@pytest.fixture
def rollback_client() -> Iterator[TestClient]:
    """让每个 HTTP 请求提交到保存点, 最后回滚外层测试事务。"""

    app = create_app()

    async def get_rollback_session(
        request: Request,
    ) -> AsyncIterator[AsyncSession]:
        connection = await request.app.state.database_engine.connect()
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()
            await connection.close()

    app.dependency_overrides[get_database_session] = get_rollback_session

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_specialist_creates_draft_case_with_audit_event(
    rollback_client: TestClient,
    existing_supplier_id: UUID,
) -> None:
    """采购专员创建草稿案件时同时得到一条关联审计事件。"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.specialist",
            "password": specialist_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    current_user_response = rollback_client.get(
        "/auth/me",
        headers=authorization,
    )
    assert current_user_response.status_code == 200
    current_user_id = current_user_response.json()["id"]

    response = rollback_client.post(
        "/api/admission/cases",
        json={"supplier_id": str(existing_supplier_id)},
        headers=authorization,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["supplier_id"] == str(existing_supplier_id)
    assert data["submitted_by_user_id"] == current_user_id
    assert data["status"] == "draft"

    audit_event = data["audit_event"]
    assert audit_event["subject_type"] == "admission_case"
    assert audit_event["subject_id"] == data["id"]
    assert audit_event["event_type"] == "admission_case_created"
    assert audit_event["actor_user_id"] == current_user_id
    assert audit_event["payload"] == {"supplier_id": str(existing_supplier_id)}


def test_procurement_manager_cannot_create_admission_case(
    rollback_client: TestClient,
    existing_supplier_id: UUID,
) -> None:
    """采购经理不能代替采购专员创建准入案件。"""

    settings = load_settings()
    manager_password = settings.demo_manager_password
    assert manager_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.manager",
            "password": manager_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        "/api/admission/cases",
        json={"supplier_id": str(existing_supplier_id)},
        headers=authorization,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "无权限创建准入案件"}


def test_specialist_gets_404_when_supplier_does_not_exist(
    rollback_client: TestClient,
) -> None:
    """采购专员不能为不存在的供应商创建准入案件。"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.specialist",
            "password": specialist_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        "/api/admission/cases",
        json={"supplier_id": str(uuid4())},
        headers=authorization,
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == {
        "code": "not_found",
        "message": "Resource not found",
    }
    assert isinstance(body["request_id"], str)
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_specialist_registers_document_metadata_with_audit_event(
    rollback_client: TestClient,
    existing_admission_case_id: UUID,
) -> None:
    """采购专员登记材料元数据时同步追加案件审计事件。"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.specialist",
            "password": specialist_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    current_user_response = rollback_client.get(
        "/auth/me",
        headers=authorization,
    )
    assert current_user_response.status_code == 200
    current_user_id = current_user_response.json()["id"]

    document_sha256 = "b" * 64
    response = rollback_client.post(
        f"/api/admission/cases/{existing_admission_case_id}/documents",
        json={
            "document_type": "business_license",
            "original_file_name": "business_license.pdf",
            "media_type": "application/pdf",
            "file_size_bytes": 2048,
            "sha256": document_sha256,
        },
        headers=authorization,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["admission_case_id"] == str(existing_admission_case_id)
    assert data["uploaded_by_user_id"] == current_user_id
    assert data["document_type"] == "business_license"
    assert data["original_file_name"] == "business_license.pdf"
    assert data["media_type"] == "application/pdf"
    assert data["file_size_bytes"] == 2048
    assert data["sha256"] == document_sha256
    assert "id" in data
    assert "created_at" in data

    audit_event = data["audit_event"]
    assert audit_event["subject_type"] == "admission_case"
    assert audit_event["subject_id"] == str(existing_admission_case_id)
    assert audit_event["event_type"] == "documents_registered"
    assert audit_event["actor_user_id"] == current_user_id
    assert audit_event["payload"] == {
        "document_count": 1,
        "document_ids": [data["id"]],
    }


def test_procurement_manager_cannot_register_document(
    rollback_client: TestClient,
    existing_admission_case_id: UUID,
) -> None:
    """采购经理不能上传准入材料。"""

    settings = load_settings()
    manager_password = settings.demo_manager_password
    assert manager_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.manager",
            "password": manager_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{existing_admission_case_id}/documents",
        json={
            "document_type": "business_license",
            "original_file_name": "manager-forbidden.pdf",
            "media_type": "application/pdf",
            "file_size_bytes": 2048,
            "sha256": "c" * 64,
        },
        headers=authorization,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "无权限登记准入材料"}


def test_specialist_gets_404_when_admission_case_does_not_exist(
    rollback_client: TestClient,
) -> None:
    """采购专员不能向不存在的准入案件登记材料。"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.specialist",
            "password": specialist_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{uuid4()}/documents",
        json={
            "document_type": "business_license",
            "original_file_name": "missing-case.pdf",
            "media_type": "application/pdf",
            "file_size_bytes": 2048,
            "sha256": "d" * 64,
        },
        headers=authorization,
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == {
        "code": "not_found",
        "message": "Resource not found",
    }
    assert isinstance(body["request_id"], str)
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_specialist_gets_409_for_duplicate_document(
    rollback_client: TestClient,
    existing_admission_case_id: UUID,
    existing_document_sha256: str,
) -> None:
    """同一案件重复登记相同 SHA-256 的材料时返回冲突。"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None

    login_response = rollback_client.post(
        "/auth/login",
        json={
            "username": "demo.specialist",
            "password": specialist_password.get_secret_value(),
        },
    )
    assert login_response.status_code == 200
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{existing_admission_case_id}/documents",
        json={
            "document_type": "business_license",
            "original_file_name": "duplicate-license.pdf",
            "media_type": "application/pdf",
            "file_size_bytes": 2048,
            "sha256": existing_document_sha256,
        },
        headers=authorization,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "该材料已登记"}
