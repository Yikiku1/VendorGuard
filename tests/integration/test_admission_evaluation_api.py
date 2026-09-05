from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission import AdmissionCase, AdmissionCaseStatus
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
    """创建并清理评估端点测试需要的候选供应商。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            supplier = Supplier(
                display_name="评估端点测试供应商",
                declared_registration_id=f"EVALAPI-{uuid4()}",
                category_code="standard_components",
            )
            session.add(supplier)
            await session.flush()
            supplier_id = supplier.id

        yield supplier_id
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(delete(Supplier).where(Supplier.id == supplier_id))
        await engine.dispose()


async def _create_case(
    session: AsyncSession,
    *,
    supplier_id: UUID,
    status: AdmissionCaseStatus,
) -> UUID:
    """在已提交事务内为端点测试创建指定状态的案件, 返回其 id。"""

    specialist = await session.scalar(select(User).where(User.username == "demo.specialist"))
    assert specialist is not None
    admission_case = AdmissionCase(
        supplier_id=supplier_id,
        submitted_by_user_id=specialist.id,
        status=status,
    )
    session.add(admission_case)
    await session.flush()
    return admission_case.id


@pytest_asyncio.fixture
async def draft_case_id(existing_supplier_id: UUID) -> AsyncIterator[UUID]:
    """创建一个 draft 案件用于评估端点正向测试。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            case_id = await _create_case(
                session, supplier_id=existing_supplier_id, status=AdmissionCaseStatus.DRAFT
            )
        yield case_id
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(delete(AdmissionCase).where(AdmissionCase.id == case_id))
        await engine.dispose()


@pytest_asyncio.fixture
async def pending_approval_case_id(existing_supplier_id: UUID) -> AsyncIterator[UUID]:
    """创建一个 pending_approval 案件用于非法起始状态测试。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            case_id = await _create_case(
                session,
                supplier_id=existing_supplier_id,
                status=AdmissionCaseStatus.PENDING_APPROVAL,
            )
        yield case_id
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(delete(AdmissionCase).where(AdmissionCase.id == case_id))
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


def _login(client: TestClient, username: str, password_key: str) -> dict[str, str]:
    """登录指定 demo 账号并返回带 Bearer Token 的请求头。"""

    settings = load_settings()
    password = getattr(settings, password_key)
    assert password is not None
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password.get_secret_value()},
    )
    assert login_response.status_code == 200
    return {"Authorization": f"Bearer {login_response.json()['access_token']}"}


def _specialist_headers(client: TestClient) -> dict[str, str]:
    """登录演示采购专员。"""

    return _login(client, "demo.specialist", "demo_specialist_password")


def _complete_facts_body(*, documents_complete: bool = True) -> dict[str, object]:
    """构造一份合法的评估请求体, 只为已给出事实配来源定位。"""

    return {
        "business_license_document_status": "valid",
        "category_required_documents_complete": documents_complete,
        "sources": {
            "business_license_document_status": "doc-license@page:1",
            "category_required_documents_complete": "doc-list@page:1",
        },
    }


def test_specialist_can_evaluate_draft_case(
    rollback_client: TestClient,
    draft_case_id: UUID,
) -> None:
    """采购专员评估事实齐全的 draft 案件, 返回 200 且案件进入 analyzing。"""

    headers = _specialist_headers(rollback_client)

    response = rollback_client.post(
        f"/api/admission/cases/{draft_case_id}/evaluation",
        json=_complete_facts_body(),
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["case_id"] == str(draft_case_id)
    assert data["case_status"] == "analyzing"
    assert [event["event_type"] for event in data["audit_events"]] == [
        "admission_case_status_changed",
        "rules_evaluated",
    ]


def test_evaluation_with_incomplete_facts_moves_to_pending_documents(
    rollback_client: TestClient,
    draft_case_id: UUID,
) -> None:
    """材料不齐事实在端点上命中 VEN-002, 案件经 analyzing 落到 pending_documents。"""

    headers = _specialist_headers(rollback_client)

    response = rollback_client.post(
        f"/api/admission/cases/{draft_case_id}/evaluation",
        json=_complete_facts_body(documents_complete=False),
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["case_status"] == "pending_documents"
    assert [event["event_type"] for event in data["audit_events"]] == [
        "admission_case_status_changed",
        "rules_evaluated",
        "rule_hit_recorded",
        "admission_case_status_changed",
    ]


def test_manager_cannot_evaluate_admission_case(
    rollback_client: TestClient,
    draft_case_id: UUID,
) -> None:
    """采购经理不能发起准入评估, 返回 403。"""

    headers = _login(rollback_client, "demo.manager", "demo_manager_password")

    response = rollback_client.post(
        f"/api/admission/cases/{draft_case_id}/evaluation",
        json=_complete_facts_body(),
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "无权限评估准入案件"}


def test_evaluation_on_missing_case_returns_404(
    rollback_client: TestClient,
) -> None:
    """评估不存在的案件走全局 404 handler, 返回统一 not_found 信封。"""

    headers = _specialist_headers(rollback_client)

    response = rollback_client.post(
        f"/api/admission/cases/{uuid4()}/evaluation",
        json=_complete_facts_body(),
        headers=headers,
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == {"code": "not_found", "message": "请求的资源不存在"}
    assert isinstance(body["request_id"], str)
    assert body["request_id"]


def test_evaluation_rejects_facts_without_source_location(
    rollback_client: TestClient,
    draft_case_id: UUID,
) -> None:
    """请求体事实缺来源定位时被 StructuredFacts 校验拒绝, 返回 422。"""

    headers = _specialist_headers(rollback_client)

    response = rollback_client.post(
        f"/api/admission/cases/{draft_case_id}/evaluation",
        json={
            "business_license_document_status": "valid",
            "category_required_documents_complete": True,
            "sources": {},
        },
        headers=headers,
    )

    assert response.status_code == 422


def test_evaluation_on_pending_approval_case_returns_409(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
) -> None:
    """非 draft/pending_documents 案件不能评估, 返回 409 且状态不变。"""

    headers = _specialist_headers(rollback_client)

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/evaluation",
        json=_complete_facts_body(),
        headers=headers,
    )

    assert response.status_code == 409
    assert "不支持从" in response.json()["detail"]

    detail_response = rollback_client.get(
        f"/api/admission/cases/{pending_approval_case_id}",
        headers=headers,
    )
    assert detail_response.json()["status"] == "pending_approval"


async def _failing_append_audit_event(*args: object, **kwargs: object) -> None:
    """模拟审计事件写入失败的替身, 用于端点事务回滚测试。"""

    raise RuntimeError("audit storage unavailable")


def test_evaluation_rolls_back_when_audit_append_fails(
    rollback_client: TestClient,
    draft_case_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计写入失败时评估的状态变化整体回滚, 案件仍是 draft。

    patch 目标为 vendorguard.admission.append_audit_event: evaluate_admission_case
    通过 from vendorguard.audit import 把名字绑定进 admission 命名空间, patch 源模块
    无效。这是 Day 4 decision 回滚测试同款陷阱。
    """

    monkeypatch.setattr(
        "vendorguard.admission.append_audit_event",
        _failing_append_audit_event,
    )

    headers = _specialist_headers(rollback_client)

    response = rollback_client.post(
        f"/api/admission/cases/{draft_case_id}/evaluation",
        json=_complete_facts_body(),
        headers=headers,
    )

    assert response.status_code == 500

    detail_response = rollback_client.get(
        f"/api/admission/cases/{draft_case_id}",
        headers=headers,
    )
    assert detail_response.json()["status"] == "draft"
