import asyncio

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from vendorguard.app import create_app
from vendorguard.logging import get_request_id


def test_request_id_is_available_inside_route() -> None:
    app = create_app()

    @app.get("/request-context")
    async def request_context_route() -> dict[str, str | None]:
        return {"request_id": get_request_id()}

    with TestClient(app) as client:
        response = client.get("/request-context")

    assert response.status_code == 200
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_request_ids_are_isolated_between_concurrent_request() -> None:
    app = create_app()

    @app.get("/concurrent-request-context")
    async def concurrent_request_context() -> dict[str, str | None]:
        before = get_request_id()
        await asyncio.sleep(0)
        after = get_request_id()

        return {"before": before, "after": after}

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        responses = await asyncio.gather(
            client.get("/concurrent-request-context"),
            client.get("/concurrent-request-context"),
        )

    request_ids: list[str] = []

    for response in responses:
        assert response.status_code == 200

        response_request_id = response.headers["X-Request-ID"]
        body = response.json()

        assert body["before"] == response_request_id
        assert body["after"] == response_request_id
        request_ids.append(response_request_id)

    assert request_ids[0] != request_ids[1]
