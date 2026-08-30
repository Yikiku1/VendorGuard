import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """读取当前异步请求上下文中的请求标识。"""

    return _request_id_context.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    """在上下文范围内绑定请求标识并在结束后恢复。"""

    token = _request_id_context.set(request_id)

    try:
        yield
    finally:
        _request_id_context.reset(token)


class JsonFormatter(logging.Formatter):
    """将日志记录格式化为包含请求标识的 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """把单条日志记录序列化为 JSON 字符串。"""

        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=UTC,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }

        return json.dumps(payload, ensure_ascii=False)


def configure_json_logger(logger: logging.Logger) -> None:
    """幂等地为指定日志器配置 JSON 输出。"""

    logger.setLevel(logging.INFO)
    logger.propagate = False

    has_json_handler = any(
        isinstance(handler.formatter, JsonFormatter) for handler in logger.handlers
    )
    if has_json_handler:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
