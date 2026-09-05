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
    """创建并清理审批测试需要的候选供应商。"""

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            supplier = Supplier(
                display_name="审批测试供应商",
                declared_registration_id=f"DECISION-{uuid4()}",
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
async def pending_approval_case_id(existing_supplier_id: UUID) -> AsyncIterator[UUID]:
    """直接创建处于 pending_approval 的案件用于测试审批端点。

    跳过 draft → pending_documents → ... 的完整链路: 案件如何进入 pending_approval
    是 Day 5 编排层的职责, 本轮测试只关心审批端点本身, 所以用 fixture 直接构造前置状态。
    """

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
                status=AdmissionCaseStatus.PENDING_APPROVAL,
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
async def draft_case_id(existing_supplier_id: UUID) -> AsyncIterator[UUID]:
    """创建处于 draft 状态的案件用于测试非法审批迁移。"""

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


def test_manager_approved_updates_case_supplier_and_emits_three_audits(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
    existing_supplier_id: UUID,
) -> None:
    """approved 时同步迁移案件与供应商资格, 并追加 3 条有序审计事件。"""

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

    current_user_response = rollback_client.get("/auth/me", headers=authorization)
    assert current_user_response.status_code == 200
    current_user_id = current_user_response.json()["id"]

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/decision",
        json={"decision": "approved", "reason": "资质完整, 同意准入"},
        headers=authorization,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["case_id"] == str(pending_approval_case_id)
    assert data["case_status"] == "approved"
    assert data["supplier_eligibility"] == "approved"

    audit_events = data["audit_events"]
    assert len(audit_events) == 3
    # 事件顺序固定为 decision_recorded -> status_changed -> eligibility_changed
    assert [event["event_type"] for event in audit_events] == [
        "admission_case_decision_recorded",
        "admission_case_status_changed",
        "supplier_eligibility_changed",
    ]

    decision_event = audit_events[0]
    assert decision_event["subject_type"] == "admission_case"
    assert decision_event["subject_id"] == str(pending_approval_case_id)
    assert decision_event["actor_user_id"] == current_user_id
    assert decision_event["payload"] == {
        "decision": "approved",
        "reason": "资质完整, 同意准入",
    }

    status_event = audit_events[1]
    assert status_event["subject_type"] == "admission_case"
    assert status_event["subject_id"] == str(pending_approval_case_id)
    assert status_event["payload"] == {
        "from_status": "pending_approval",
        "to_status": "approved",
    }

    eligibility_event = audit_events[2]
    assert eligibility_event["subject_type"] == "supplier"
    assert eligibility_event["subject_id"] == str(existing_supplier_id)
    assert eligibility_event["payload"] == {
        "from_eligibility": "candidate",
        "to_eligibility": "approved",
        "reason": "资质完整, 同意准入",
    }


def test_manager_rejected_updates_case_supplier_and_emits_three_audits(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
    existing_supplier_id: UUID,
) -> None:
    """rejected 时案件与供应商同步落到 rejected, 事件顺序与 approved 完全一致。"""

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/decision",
        json={"decision": "rejected", "reason": "营业执照过期, 拒绝准入"},
        headers=authorization,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["case_status"] == "rejected"
    assert data["supplier_eligibility"] == "rejected"

    audit_events = data["audit_events"]
    assert len(audit_events) == 3
    assert [event["event_type"] for event in audit_events] == [
        "admission_case_decision_recorded",
        "admission_case_status_changed",
        "supplier_eligibility_changed",
    ]
    assert audit_events[1]["payload"] == {
        "from_status": "pending_approval",
        "to_status": "rejected",
    }
    assert audit_events[2]["payload"]["to_eligibility"] == "rejected"


def test_manager_supplement_requested_only_updates_case_emits_two_audits(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
) -> None:
    """supplement_requested 只回退案件, 供应商资格保持 candidate 且只有 2 条审计事件。"""

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/decision",
        json={"decision": "supplement_requested", "reason": "缺少质量证书"},
        headers=authorization,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["case_status"] == "pending_documents"
    # supplier_eligibility 只在 approved/rejected 时有值
    assert data["supplier_eligibility"] is None

    audit_events = data["audit_events"]
    # 只有 decision_recorded 与 status_changed, 没有 supplier_eligibility_changed
    assert len(audit_events) == 2
    assert [event["event_type"] for event in audit_events] == [
        "admission_case_decision_recorded",
        "admission_case_status_changed",
    ]
    assert audit_events[1]["payload"] == {
        "from_status": "pending_approval",
        "to_status": "pending_documents",
    }


def test_specialist_cannot_record_decision(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
) -> None:
    """采购专员不能审批自己或其他人提交的准入案件。"""

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/decision",
        json={"decision": "approved", "reason": "越权测试"},
        headers=authorization,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "无权限审批准入案件"}


def test_decision_on_draft_case_returns_409(
    rollback_client: TestClient,
    draft_case_id: UUID,
) -> None:
    """非 pending_approval 状态的案件不能被审批, 返回 409 且不落库。"""

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{draft_case_id}/decision",
        json={"decision": "approved", "reason": "非法状态测试"},
        headers=authorization,
    )

    assert response.status_code == 409
    assert "不支持的审批决定" in response.json()["detail"]

    detail_response = rollback_client.get(
        f"/api/admission/cases/{draft_case_id}",
        headers=authorization,
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "draft"


def test_empty_reason_returns_422(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
) -> None:
    """reason 为空字符串时被 Pydantic min_length=1 拦截, 返回 422。"""

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/decision",
        json={"decision": "approved", "reason": ""},
        headers=authorization,
    )

    assert response.status_code == 422


def test_decision_on_missing_case_returns_404(
    rollback_client: TestClient,
) -> None:
    """审批不存在的案件走全局 404 handler, 返回统一 not_found 结构。"""

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{uuid4()}/decision",
        json={"decision": "approved", "reason": "不存在的案件"},
        headers=authorization,
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == {
        "code": "not_found",
        "message": "请求的资源不存在",
    }
    assert isinstance(body["request_id"], str)
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


async def _failing_append_audit_event(*args: object, **kwargs: object) -> None:
    """模拟审计事件写入失败的替身, 用于事务回滚测试。"""

    raise RuntimeError("audit storage unavailable")


def test_decision_rolls_back_when_audit_append_fails(
    rollback_client: TestClient,
    pending_approval_case_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计事件写入失败时业务修改必须整体回滚, 案件与供应商保持原状。

    monkeypatch 目标使用 vendorguard.admission.append_audit_event 而不是源模块
    vendorguard.audit.append_audit_event, 因为业务函数通过 `from vendorguard.audit
    import append_audit_event` 已经把名字绑定到 admission 模块的命名空间, patch
    源模块不会影响已导入的引用。这是 Python 单测里最容易踩的一个 patch target 陷阱。
    """

    monkeypatch.setattr(
        "vendorguard.admission.append_audit_event",
        _failing_append_audit_event,
    )

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
    authorization = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    response = rollback_client.post(
        f"/api/admission/cases/{pending_approval_case_id}/decision",
        json={"decision": "approved", "reason": "回滚测试"},
        headers=authorization,
    )

    # 未捕获异常走 app 的 500 handler
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {
        "code": "internal_server_error",
        "message": "服务器内部错误",
    }

    # 再次 GET 案件详情, 断言业务修改没落库
    detail_response = rollback_client.get(
        f"/api/admission/cases/{pending_approval_case_id}",
        headers=authorization,
    )
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["status"] == "pending_approval"
    assert detail["supplier"]["eligibility_status"] == "candidate"
