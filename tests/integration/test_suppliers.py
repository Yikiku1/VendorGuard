from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete

from vendorguard.app import create_app
from vendorguard.config import load_settings
from vendorguard.database import create_database_engine, create_session_factory
from vendorguard.suppliers import Supplier


@pytest_asyncio.fixture
async def created_registration_ids() -> AsyncIterator[list[str]]:
    """清理当前测试创建的供应商, 避免污染本地数据库。"""

    registration_ids: list[str] = []
    yield registration_ids

    if not registration_ids:
        return

    engine = create_database_engine(load_settings())
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(Supplier).where(Supplier.declared_registration_id.in_(registration_ids))
            )
    finally:
        await engine.dispose()


def test_create_supplier_success(
    created_registration_ids: list[str],
) -> None:
    """采购专员可以成功创建供应商"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None
    password = specialist_password.get_secret_value()

    with TestClient(create_app()) as client:
        # 先登录获取真实 Token
        login_response = client.post(
            "/auth/login",
            json={
                "username": "demo.specialist",
                "password": password,
            },
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]

        # 创建供应商 (使用唯一ID避免重复)
        unique_id = str(uuid4())[:8].upper()
        registration_id = f"91440300TEST{unique_id}"
        created_registration_ids.append(registration_id)
        response = client.post(
            "/api/admission/suppliers",
            json={
                "display_name": f"深圳测试科技有限公司-{unique_id}",
                "declared_registration_id": registration_id,
                "category_code": "electronic_components",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    data = response.json()
    assert "深圳测试科技有限公司" in data["display_name"]
    assert data["declared_registration_id"] == registration_id
    assert data["eligibility_status"] == "candidate"
    assert "id" in data
    assert "created_at" in data


def test_create_supplier_duplicate_registration_id(
    created_registration_ids: list[str],
) -> None:
    """相同统一社会信用代码返回 409 冲突"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None
    password = specialist_password.get_secret_value()

    # 使用唯一ID
    unique_id = str(uuid4())[:8].upper()
    registration_id = f"91440300DUP{unique_id}"
    created_registration_ids.append(registration_id)
    payload = {
        "display_name": f"深圳重复测试公司-{unique_id}",
        "declared_registration_id": registration_id,
        "category_code": "raw_materials",
    }

    with TestClient(create_app()) as client:
        # 先登录获取真实 Token
        login_response = client.post(
            "/auth/login",
            json={
                "username": "demo.specialist",
                "password": password,
            },
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]

        # 第一次创建成功
        response1 = client.post(
            "/api/admission/suppliers",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response1.status_code == 201

        # 第二次创建相同信用代码
        response2 = client.post(
            "/api/admission/suppliers",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response2.status_code == 409
        assert "统一社会信用代码已存在" in response2.json()["detail"]


def test_create_supplier_without_auth() -> None:
    """未认证用户被拒绝"""

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/admission/suppliers",
            json={
                "display_name": "测试公司",
                "declared_registration_id": "91440300NOAUTH0001",
                "category_code": "services",
            },
        )

    assert response.status_code == 401


def test_procurement_manager_cannot_create_supplier(
    created_registration_ids: list[str],
) -> None:
    """采购经理不能创建供应商。"""

    settings = load_settings()
    manager_password = settings.demo_manager_password
    assert manager_password is not None

    registration_id = f"91440300MANAGER{str(uuid4())[:8].upper()}"
    created_registration_ids.append(registration_id)

    with TestClient(create_app()) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "demo.manager",
                "password": manager_password.get_secret_value(),
            },
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]

        response = client.post(
            "/api/admission/suppliers",
            json={
                "display_name": "经理无权创建的测试供应商",
                "declared_registration_id": registration_id,
                "category_code": "electronic_components",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "无权限创建供应商"
