import asyncio
import json
import logging
from io import StringIO

from vendorguard.logging import (
    JsonFormatter,
    bind_request_id,
    configure_json_logger,
    get_request_id,
)


async def test_request_ids_are_isolated_between_async_tasks() -> None:
    async def capture_request_id(request_id: str) -> str | None:
        with bind_request_id(request_id):
            await asyncio.sleep(0)
            return get_request_id()

    results = await asyncio.gather(
        capture_request_id("request-a"),
        capture_request_id("request-b"),
    )

    assert results[0] == "request-a"
    assert results[1] == "request-b"
    assert get_request_id() is None


def test_json_formatter_includes_request_id() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.Logger("vendorguard.test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    with bind_request_id("request-123"):
        logger.info("request completed")

    payload = json.loads(stream.getvalue())

    assert payload["level"] == "INFO"
    assert payload["logger"] == "vendorguard.test"
    assert payload["message"] == "request completed"
    assert payload["request_id"] == "request-123"
    assert isinstance(payload["timestamp"], str)


def test_configure_json_logger_is_idempotent() -> None:
    logger = logging.Logger("vendorguard.test")

    configure_json_logger(logger)
    configure_json_logger(logger)

    assert logger.level == logging.INFO
    assert logger.propagate is False
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
