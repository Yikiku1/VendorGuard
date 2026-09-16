"""审查工作台 HTTP 接口: 发起, 列表, 详情与材料.

M4 方案第 7 节. 所有接口都走现有 Bearer Token (`get_current_user`); 记录不存在或不属于
当前用户统一 404, 不泄露"这条记录存在但不是你的".

本模块只做 HTTP 转换: 表单与上传校验, 调用审查应用层与本地记录仓库, 把状态映射成响应.
工具定义, 纠正预算, 检索白名单与报告闸门都在应用层与 Agent 循环里, 这里一行都不复制.

一轮审查是同步的: 用 `run_in_threadpool()` 把它放到线程里跑, 不阻塞事件循环 (方案 4.2).
记录仓库的读写是本地小文件操作, 直接调用.

错误体统一成 `{"error": {"code", "message", "retryable"}, "request_id"}` (方案 7.5):
本步用到的是未认证 (401), 参数错误 (400), 上传过大 (413) 与记录不存在 (404); 补充与
反馈接口在 M4-4 接入同一套错误体, 那时的状态冲突用 409. 认证与其它既有接口的错误形状
不受影响——它们不是 M4 的接口.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent import AgentRunOutcome
from .dependencies import get_current_user
from .materials import MaterialReadError, read_text_pdf
from .review_application import ReviewCommand, ReviewRuntime, run_review_round
from .review_records import (
    AllowedAction,
    LocalReviewStore,
    ReviewFailure,
    ReviewNotFoundError,
    ReviewRecord,
    ReviewRoundRecord,
    ReviewStatus,
    ReviewStoreError,
    ReviewSummary,
)
from .security import User

REVIEW_PATH_PREFIX = "/api/reviews"

# 方案 7.1 与 7.2 的硬数字: 上传上限 10 MB, 审查要求 1..1000 字, 列表 1..50 条.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_REQUEST_TEXT_CHARS = 1000
MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 50
DEFAULT_LIST_LIMIT = 20

# 材料层的失败码 → 记录里的稳定错误码与用户提示. 重跑同一份字节没有意义, 所以材料
# 问题一律 retryable=False: 用户要用另一份材料发起新的审查.
_MATERIAL_FAILURES: dict[str, tuple[str, str]] = {
    "file_not_found": ("material_file_not_found", "材料文件不存在, 请重新发起一次审查"),
    "not_a_pdf": ("material_not_a_pdf", "材料不是可读的 PDF 文件"),
    "scanned_pdf_unsupported": (
        "material_scanned_unsupported",
        "扫描件暂不支持, 请提交带文本层的 PDF",
    ),
    "empty_material": ("material_empty", "材料没有可核对的文本内容"),
}


class ReviewApiError(Exception):
    """审查接口的请求级错误, 由统一错误体处理器转成 JSON 响应.

    只在"没有创建或更新审查记录"时使用 (方案 7.5). 记录已经落盘之后的失败写进记录的
    `failure` 字段, 随记录视图返回, 不走 HTTP 错误分支——失败历史因此不会丢.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class ReviewDetail(ReviewRecord):
    """详情响应: 完整记录加上服务端算出的可执行动作.

    `allowed_actions` 是按当前状态算出来的视图字段 (来自 `ReviewRecord.available_actions()`),
    不写进 record.json (方案第 7 节); 页面只按它显示按钮, 不在 JavaScript 里复制状态转换规则.
    """

    allowed_actions: list[AllowedAction] = Field(default_factory=list)


class ReviewListItem(BaseModel):
    """列表项: 只回答"看哪一条", 不带轮次, 工具事件与材料全文 (方案 7.2)."""

    model_config = ConfigDict(extra="forbid")

    review_id: UUID
    original_filename: str
    status: ReviewStatus
    created_at: datetime
    request_excerpt: str
    finding_count: int
    retry_of_review_id: UUID | None

    @classmethod
    def from_summary(cls, summary: ReviewSummary) -> ReviewListItem:
        """按接口契约裁掉仓库摘要里的内部字段 (所有者 ID 不上列表)."""

        return cls(
            review_id=summary.review_id,
            original_filename=summary.original_filename,
            status=summary.status,
            created_at=summary.created_at,
            request_excerpt=summary.request_excerpt,
            finding_count=summary.finding_count,
            retry_of_review_id=summary.retry_of_review_id,
        )


router = APIRouter(prefix=REVIEW_PATH_PREFIX, tags=["审查工作台"])


def get_review_runtime(request: Request) -> ReviewRuntime:
    """取应用启动时装载的审查依赖; 测试用 `app.dependency_overrides` 换成替身."""

    return cast(ReviewRuntime, request.app.state.review_runtime)


def get_review_store(request: Request) -> LocalReviewStore:
    """取本地的审查记录仓库, 同上."""

    return cast(LocalReviewStore, request.app.state.review_store)


@router.post("", response_model=ReviewDetail, status_code=status.HTTP_201_CREATED)
async def create_review(
    user: Annotated[User, Depends(get_current_user)],
    store: Annotated[LocalReviewStore, Depends(get_review_store)],
    runtime: Annotated[ReviewRuntime, Depends(get_review_runtime)],
    material: Annotated[UploadFile | None, File()] = None,
    request_text: Annotated[str | None, Form()] = None,
    reference_date: Annotated[str | None, Form()] = None,
) -> ReviewDetail:
    """发起一次审查: 先建档并保存材料, 再在本地线程里跑一轮.

    201 表示"审查记录已经创建", 不表示一定产出了报告: 扫描件这类材料错误也会留下一条
    `failed` 记录并返回, 调用方按 `status` 与 `failure` 展示 (方案 7.1). 缺少表单字段,
    日期格式错误与上传超限都发生在建档之前, 一律 4xx 且不创建记录.
    """

    payload = await _read_upload(material)
    text = _validated_request_text(request_text)
    day = _validated_reference_date(reference_date)

    record = store.create(
        owner_user_id=user.id,
        request_text=text,
        reference_date=day,
        original_filename=_display_filename(material),
        material_bytes=payload,
    )

    try:
        document = read_text_pdf(
            store.material_path_for_owner(record.review_id, owner_user_id=user.id)
        )
    except MaterialReadError as exc:
        code, message = _material_failure(exc)
        failed = store.mutate_for_owner(
            record.review_id,
            owner_user_id=user.id,
            mutate=lambda current: current.with_failure(
                code=code, message=message, retryable=False
            ),
        )
        return _detail(failed)

    record = store.mutate_for_owner(
        record.review_id,
        owner_user_id=user.id,
        mutate=lambda current: current.with_material(document),
    )

    started_at = datetime.now(UTC)
    command = ReviewCommand(
        user_request=record.request_text,
        reference_date=record.reference_date,
        material=document,
    )
    outcome = await run_in_threadpool(run_review_round, command, runtime=runtime)
    finished_at = datetime.now(UTC)

    round_record = ReviewRoundRecord.from_outcome(
        outcome,
        number=len(record.rounds) + 1,
        model=runtime.model_name,
        started_at=started_at,
        finished_at=finished_at,
    )
    saved = store.mutate_for_owner(
        record.review_id,
        owner_user_id=user.id,
        mutate=lambda current: current.with_round(round_record, failure=_agent_failure(outcome)),
    )
    return _detail(saved)


@router.get("", response_model=list[ReviewListItem])
async def list_reviews(
    user: Annotated[User, Depends(get_current_user)],
    store: Annotated[LocalReviewStore, Depends(get_review_store)],
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ReviewListItem]:
    """列出当前用户最近的审查记录, 按创建时间倒序; limit 只接受 1 到 50."""

    if not MIN_LIST_LIMIT <= limit <= MAX_LIST_LIMIT:
        raise ReviewApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_request",
            message=f"limit 必须在 {MIN_LIST_LIMIT} 到 {MAX_LIST_LIMIT} 之间",
        )
    summaries = store.list_for_owner(owner_user_id=user.id, limit=limit)
    return [ReviewListItem.from_summary(item) for item in summaries]


@router.get("/{review_id}", response_model=ReviewDetail)
async def get_review(
    review_id: str,
    user: Annotated[User, Depends(get_current_user)],
    store: Annotated[LocalReviewStore, Depends(get_review_store)],
) -> ReviewDetail:
    """取一条记录的完整视图: 所有轮次, 工具事件, 报告, 反馈与可执行动作."""

    return _detail(_read_for_owner(store, review_id, user))


@router.get("/{review_id}/material")
async def read_review_material(
    review_id: str,
    user: Annotated[User, Depends(get_current_user)],
    store: Annotated[LocalReviewStore, Depends(get_review_store)],
) -> Response:
    """返回这条记录保存的原始材料 PDF; 页面用带 Token 的 fetch 取 Blob (方案 7.2)."""

    try:
        payload = store.read_material_bytes(review_id, owner_user_id=user.id)
    except ReviewStoreError as exc:
        raise _store_error(exc) from exc
    return Response(content=payload, media_type="application/pdf")


def register_review_error_handlers(app: FastAPI) -> None:
    """把审查接口的错误映射注册到应用 (方案 7.5).

    只影响审查接口路径: 登录等既有接口保持原来的错误形状, 它们不是 M4 的接口.
    """

    app.add_exception_handler(ReviewApiError, _review_error_response)
    app.add_exception_handler(RequestValidationError, _validation_error_response)
    app.add_exception_handler(status.HTTP_401_UNAUTHORIZED, _unauthorized_response)


async def _review_error_response(request: Request, exc: Exception) -> JSONResponse:
    """审查接口的请求级错误: 统一错误体, 只带用户能看懂的信息."""

    error = cast(ReviewApiError, exc)
    return _error_response(
        request,
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        retryable=error.retryable,
    )


async def _unauthorized_response(request: Request, exc: Exception) -> Response:
    """未认证: 审查接口用统一错误体, 其它接口保持 FastAPI 默认的 detail 形状."""

    if not _is_review_path(request):
        return await http_exception_handler(request, cast(HTTPException, exc))
    return _error_response(
        request,
        status_code=status.HTTP_401_UNAUTHORIZED,
        code="unauthenticated",
        message="需要有效的访问令牌",
        retryable=False,
        headers=cast(HTTPException, exc).headers,
    )


async def _validation_error_response(request: Request, exc: Exception) -> Response:
    """审查接口收到形状不对的请求 (例如用 JSON 调用上传接口) 也报 400 与统一错误体."""

    if not _is_review_path(request):
        return await request_validation_exception_handler(
            request, cast(RequestValidationError, exc)
        )
    return _error_response(
        request,
        status_code=status.HTTP_400_BAD_REQUEST,
        code="invalid_request",
        message="请求参数不合法",
        retryable=False,
    )


def _is_review_path(request: Request) -> bool:
    """请求是否落在审查接口前缀下: 只有这些接口用 M4 的统一错误体."""

    return request.url.path.startswith(REVIEW_PATH_PREFIX)


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """按方案 7.5 组装错误体: 稳定错误码, 用户提示, 能否重跑与请求 ID."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message, "retryable": retryable},
            "request_id": request.state.request_id,
        },
        headers=headers,
    )


async def _read_upload(material: UploadFile | None) -> bytes:
    """读取上传内容: 最多读 10 MB + 1 字节, 按实际字节数判超限.

    不信任 Content-Type 与原始文件名: 能不能读由材料层判定, 文件名只作展示.
    """

    if material is None:
        raise ReviewApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_request",
            message="必须上传一份 PDF 材料",
        )
    payload = await material.read(MAX_UPLOAD_BYTES + 1)
    await material.close()
    if not payload:
        raise ReviewApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_request",
            message="上传的材料是空文件",
        )
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ReviewApiError(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="upload_too_large",
            message=f"材料不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    return payload


def _validated_request_text(request_text: str | None) -> str:
    """审查要求: 去首尾空白后 1 到 1000 字 (方案 7.1)."""

    text = (request_text or "").strip()
    if not text:
        raise ReviewApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_request",
            message="审查要求不能为空",
        )
    if len(text) > MAX_REQUEST_TEXT_CHARS:
        raise ReviewApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_request",
            message=f"审查要求最多 {MAX_REQUEST_TEXT_CHARS} 个字符",
        )
    return text


def _validated_reference_date(reference_date: str | None) -> date:
    """参考日期必须是 ISO 日期 (演示固定用 2026-09-01)."""

    try:
        return date.fromisoformat((reference_date or "").strip())
    except ValueError as exc:
        raise ReviewApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_request",
            message="参考日期格式应为 YYYY-MM-DD",
        ) from exc


def _display_filename(material: UploadFile | None) -> str:
    """上传文件名只作展示: 去掉任何路径部分, 空名字给一个固定占位."""

    raw = (material.filename or "") if material is not None else ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name if name and name not in {".", ".."} else "material.pdf"


def _material_failure(exc: MaterialReadError) -> tuple[str, str]:
    """材料层的失败码 → (记录里的稳定错误码, 用户提示)."""

    return _MATERIAL_FAILURES.get(exc.code, (f"material_{exc.code}", str(exc)))


def _agent_failure(outcome: AgentRunOutcome) -> ReviewFailure | None:
    """Agent 失败的记录级信息, 非失败轮次返回 None.

    模型失败, 检索配置失败与预算耗尽都写进这里, 共用一个稳定错误码, 具体原因在
    `message` 里 (Agent 的失败文案); 区分它们要靠解析文案, 不做. 这类失败可以用原记录
    重跑, 所以 retryable=True (方案 7.4).
    """

    if outcome.kind != "failed":
        return None
    return ReviewFailure(code="agent_run_failed", message=outcome.text, retryable=True)


def _store_error(exc: ReviewStoreError) -> ReviewApiError:
    """记录层的错误 → 接口错误: 不存在与不属于当前用户都归成 404."""

    if isinstance(exc, ReviewNotFoundError):
        return ReviewApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="review_not_found",
            message="审查记录不存在",
        )
    return ReviewApiError(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_server_error",
        message="审查记录读写失败",
    )


def _read_for_owner(store: LocalReviewStore, review_id: str, user: User) -> ReviewRecord:
    """读一条属于该用户的记录, 不存在与不属于他都报同一种接口错误."""

    try:
        return store.get_for_owner(review_id, owner_user_id=user.id)
    except ReviewStoreError as exc:
        raise _store_error(exc) from exc


def _detail(record: ReviewRecord) -> ReviewDetail:
    """把记录投影成详情响应: allowed_actions 只在响应里出现, 不落盘."""

    return ReviewDetail(**record.model_dump(), allowed_actions=list(record.available_actions()))


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_UPLOAD_BYTES",
    "ReviewApiError",
    "ReviewDetail",
    "ReviewListItem",
    "get_review_runtime",
    "get_review_store",
    "register_review_error_handlers",
    "router",
]
