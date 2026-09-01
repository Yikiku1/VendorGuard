import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission.routes import router as admission_router
from vendorguard.config import load_settings
from vendorguard.database import (
    create_database_engine,
    create_session_factory,
    get_database_session,
)
from vendorguard.dependencies import get_current_user
from vendorguard.logging import bind_request_id, configure_json_logger
from vendorguard.security import (
    User,
    UserRole,
    authenticate_user,
    create_access_token,
)


class LoginRequest(BaseModel):
    """表示登录接口接受的用户名和密码"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """表示登录成功后返回的 Bearer Token"""

    access_token: str
    token_type: str


class CurrentUserResponse(BaseModel):
    """表示当前已认证用户的公开身份信息。"""

    id: UUID
    username: str
    role: UserRole


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

    @app.post("/auth/login", response_model=TokenResponse)
    async def login(
        credentials: LoginRequest,
        session: Annotated[
            AsyncSession,
            Depends(get_database_session),
        ],
    ) -> TokenResponse:
        """验证用户凭据并签发访问令牌"""

        user = await authenticate_user(
            session,
            username=credentials.username,
            password=credentials.password,
        )

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="账号或密码错误(无效)",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return TokenResponse(
            access_token=create_access_token(user, settings),
            token_type="bearer",
        )

    @app.get("/auth/me", response_model=CurrentUserResponse)
    async def current_user(
        user: Annotated[User, Depends(get_current_user)],
    ) -> CurrentUserResponse:
        """返回当前已认证用户的信息"""

        return CurrentUserResponse(
            id=user.id,
            username=user.username,
            role=user.role,
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

    app.include_router(admission_router)
    return app
