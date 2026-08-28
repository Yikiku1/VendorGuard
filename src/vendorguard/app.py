import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from vendorguard.config import load_settings
from vendorguard.logging import bind_request_id, configure_json_logger


def create_app() -> FastAPI:
    settings = load_settings()

    http_logger = logging.getLogger("vendorguard.http")
    configure_json_logger(http_logger)

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
    )

    @app.middleware("http")
    async def add_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
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
        return {"status": "ok"}

    return app
