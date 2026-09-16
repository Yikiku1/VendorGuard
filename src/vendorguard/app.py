import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from dotenv import dotenv_values
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
from vendorguard.review_application import build_review_runtime
from vendorguard.review_records import LocalReviewStore
from vendorguard.review_routes import register_review_error_handlers
from vendorguard.review_routes import router as review_router
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
        """在应用启动和关闭时创建并释放数据库与审查依赖。"""

        engine = create_database_engine(settings)

        try:
            app.state.database_engine = engine
            app.state.session_factory = create_session_factory(engine)
            # M4: 审查依赖也在这里装载一次。片段清单、向量缓存或模型配置不可用就启动
            # 失败, 不能让页面启动后把配置问题说成"制度里没有依据" (M4 方案 5.2)。
            app.state.review_store = LocalReviewStore(settings.review_data_dir)
            app.state.review_runtime = build_review_runtime(
                {**dotenv_values(".env"), **os.environ},
                app_settings=settings,
            )
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
                detail="账号或密码错误",
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
                http_logger.error("请求处理失败")
                raise
            else:
                http_logger.info("请求处理完成")

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
                    "message": "请求的资源不存在",
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
                    "message": "服务器内部错误",
                },
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        """返回应用进程的基础健康状态。"""

        return {"status": "ok"}

    register_review_error_handlers(app)
    app.include_router(admission_router)
    app.include_router(review_router)
    return app
