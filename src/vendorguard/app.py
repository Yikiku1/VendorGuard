import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from vendorguard.config import load_settings
from vendorguard.database import create_database_engine, create_session_factory
from vendorguard.logging import bind_request_id, configure_json_logger


def create_app() -> FastAPI:
    """创建并配置 VendorGuard FastAPI 应用。"""

    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """在应用启动和关闭时创建并释放数据库资源。"""

        engine = create_database_engine(settings)

        try:
            app.state.database_engine = engine
            app.state.session_factory = create_session_factory(engine)
            yield
        finally:
            await engine.dispose()

    http_logger = logging.getLogger("vendorguard.http")
    configure_json_logger(http_logger)

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def add_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """为每个请求绑定标识并记录统一的完成或失败日志。"""

        request_id = str(uuid4())
        request.state.request_id = request_id

        with bind_request_id(request_id):
            try:
                response = await call_next(request)
            except Exception:
                http_logger.error("request failed")
                raise
            else:
                http_logger.info("request completed")

        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(status.HTTP_404_NOT_FOUND)
    async def not_found_handler(
        request: Request,
        _: Exception,
    ) -> JSONResponse:
        """返回包含请求标识的统一 404 JSON 响应。"""

        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "not_found",
                    "message": "Resource not found",
                },
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(Exception)
    async def unhandler_exception_handler(
        request: Request,
        _: Exception,
    ) -> JSONResponse:
        """返回不泄露内部异常细节的统一 500 JSON 响应。"""

        request_id: str = request.state.request_id

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_server_error",
                    "message": "Internal server error",
                },
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        """返回应用进程的基础健康状态。"""

        return {"status": "ok"}

    return app
