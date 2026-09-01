from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.app import create_app
from vendorguard.config import load_settings
from vendorguard.database import (
    create_database_engine,
    create_session_factory,
    get_database_session,
)
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
