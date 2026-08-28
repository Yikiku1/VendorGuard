import json
import logging
from io import StringIO

from fastapi.testclient import TestClient

from vendorguard.app import create_app
from vendorguard.logging import JsonFormatter


def test_request_log_matches_response_request_id() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("vendorguard.http")
    original_handlers = logger.handlers.copy()
    original_level = logger.level
    original_propagate = logger.propagate

    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        with TestClient(create_app()) as client:
            response = client.get("/health")
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    lines = stream.getvalue().splitlines()

    assert len(lines) == 1

    payload = json.loads(lines[0])

    assert payload["message"] == "request completed"
    assert payload["request_id"] == response.headers["X-Request-ID"]


def test_failed_request_log_matches_safe_response() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("vendorguard.http")
    original_handlers = logger.handlers.copy()
    original_level = logger.level
    original_propagate = logger.propagate

    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        app = create_app()

        @app.get("/broken-logging")
        def broken_logging_route() -> None:
            raise RuntimeError("sensitive-internal-detail")

        with TestClient(
            app,
            raise_server_exceptions=False,
        ) as client:
            response = client.get("/broken-logging")
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    lines = stream.getvalue().splitlines()

    assert response.status_code == 500
    assert len(lines) == 1

    payload = json.loads(lines[0])
    body = response.json()

    assert payload["level"] == "ERROR"
    assert payload["message"] == "request failed"
    assert payload["request_id"] == body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert "sensitive-internal-detail" not in response.text
    assert "sensitive-internal-detail" not in stream.getvalue()
