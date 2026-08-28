from fastapi.testclient import TestClient

from vendorguard.app import create_app


def test_not_found_returns_standard_error_response() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/does-not-exist")

    body = response.json()

    assert response.status_code == 404
    assert body["error"] == {
        "code": "not_found",
        "message": "Resource not found",
    }
    assert isinstance(body["request_id"], str)
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_unhandled_exception_returns_safe_response() -> None:
    app = create_app()

    @app.get("/broken")
    def broken_route() -> None:
        raise RuntimeError("sensitive-internal-detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/broken")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")

    body = response.json()

    assert body["error"] == {
        "code": "internal_server_error",
        "message": "Internal server error",
    }
    assert "sensitive-internal-detail" not in response.text
    assert response.headers["X-Request-ID"] == body["request_id"]
