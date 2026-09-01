from uuid import uuid4

from fastapi.testclient import TestClient

from vendorguard.app import create_app
from vendorguard.config import load_settings


def test_create_supplier_success() -> None:
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
        response = client.post(
            "/api/admission/suppliers",
            json={
                "display_name": f"深圳测试科技有限公司-{unique_id}",
                "declared_registration_id": f"91440300TEST{unique_id}",
                "category_code": "electronic_components",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    data = response.json()
    assert "深圳测试科技有限公司" in data["display_name"]
    assert data["declared_registration_id"].startswith("91440300TEST")
    assert data["eligibility_status"] == "candidate"
    assert "id" in data
    assert "created_at" in data


def test_create_supplier_duplicate_registration_id() -> None:
    """相同统一社会信用代码返回 409 冲突"""

    settings = load_settings()
    specialist_password = settings.demo_specialist_password
    assert specialist_password is not None
    password = specialist_password.get_secret_value()

    # 使用唯一ID
    unique_id = str(uuid4())[:8].upper()
    payload = {
        "display_name": f"深圳重复测试公司-{unique_id}",
        "declared_registration_id": f"91440300DUP{unique_id}",
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
