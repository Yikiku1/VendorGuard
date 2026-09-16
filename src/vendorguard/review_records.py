"""本地审查记录: Schema, 原子读写, 所有权与状态转换.

M4 方案 4.3 与第 6 节: 审查记录保存在本地忽略目录, 是**演示证据而不是业务案件**——
它用独立的 `review_id` 与 `owner_user_id`, 不引用 `admission_cases`, 也不复用人工
准入决定接口. 每条记录的目录固定为:

    <root>/<review_id>/
        material.pdf
        record.json

设计要点:

- `review_id` 由服务端生成 UUID, 外部传入的值只按 UUID 解析: 路径片段一律按"记录
  不存在"处理, 因此不能借它越出根目录; 上传文件名只作展示字段, 不参与路径计算.
- `record.json` 用 UTF-8 与显式 `schema_version`; 写盘先写同目录临时文件再原子替换,
  替换失败时旧记录保持不变, 临时文件一并清理.
- 所有读取与修改都核对 `owner_user_id`: 别人的记录与不存在的记录抛同一种错误,
  让接口统一回 404, 不泄露"这条记录存在".
- 同一 `review_id` 的"检查状态 + 写盘"都在 `mutate_for_owner()` 的同一次临界区内.
  原子替换只解决"半份文件", 串行化才解决"丢失更新"; 本地演示固定单个 worker,
  多进程锁与共享存储不在本阶段范围内.
- 状态机 (方案 6.3):

      create -> running -> completed
                        -> question -> running -> completed
                                              -> failed
                        -> failed

  `question` 只能补充一次; 只有 `completed` 能提交一次反馈, 且反馈不能覆盖.

工具事件在写进记录前继续执行运行记录的 `sk-` 脱敏兜底: 记录会被工作台读取并展示,
密钥样式的字符串不能跟着进去.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from .agent import AgentRunOutcome, redact_secret
from .agent_report import ReviewReport
from .materials import MaterialDocument

# 首页 Schema 版本: 显式写进 record.json, 将来改结构时按它判断能不能读.
SCHEMA_VERSION: Final = "1.0"
RECORD_FILENAME = "record.json"
# 材料文件固定命名, 与上传文件名无关: 上传文件名只作展示字段.
MATERIAL_FILENAME = "material.pdf"
# 列表里的请求摘要长度: 列表只回答"看哪一条", 全文与工具事件都在详情里.
REQUEST_EXCERPT_CHARS = 60
# 临时文件与正式文件同目录, 保证 os.replace 是同文件系统上的原子操作.
_TEMP_FILENAME = ".record.json.tmp"

# 页面可执行的动作: 由记录状态算出, 作为详情响应的视图字段返回, 不写进 record.json.
AllowedAction = Literal["supplement", "feedback", "rerun"]


class ReviewStoreError(ValueError):
    """本地记录读写失败, 或记录内容与当前 Schema 不符时抛出的错误."""


class ReviewNotFoundError(ReviewStoreError):
    """记录不存在, 不属于当前用户, 或 review_id 不是合法 UUID.

    三种情况故意用同一个错误: 接口据此统一返回 404, 不泄露"这条记录存在但不是你的".
    """


class ReviewStateError(ReviewStoreError):
    """对记录的操作与它当前的状态不符 (非法状态转换) 时抛出的错误."""


class ReviewStatus(StrEnum):
    """一条本地审查记录的状态 (方案 6.3)."""

    RUNNING = "running"
    QUESTION = "question"
    COMPLETED = "completed"
    FAILED = "failed"


_STATUS_BY_KIND = {
    "answer": ReviewStatus.COMPLETED,
    "question": ReviewStatus.QUESTION,
    "failed": ReviewStatus.FAILED,
}


class ReviewFailure(BaseModel):
    """一次失败的稳定错误码, 给用户看的提示, 以及能否用原记录重跑."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class ReviewFeedback(BaseModel):
    """一次报告反馈: 只有确认或要求重查, 一旦保存不能覆盖."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["confirmed", "recheck_requested"]
    comment: str = ""
    created_at: AwareDatetime


class ReviewRoundRecord(BaseModel):
    """一次 Agent 运行的完整结果: 它自带这一轮的工具事件与报告, 不跨轮共享."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    model: str = Field(min_length=1)
    kind: Literal["answer", "question", "failed"]
    text: str
    model_requests: int = Field(ge=0)
    tool_attempts: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    tool_events: tuple[dict[str, Any], ...] = ()
    report: ReviewReport | None = None

    @classmethod
    def from_outcome(
        cls,
        outcome: AgentRunOutcome,
        *,
        number: int,
        model: str,
        started_at: datetime,
        finished_at: datetime,
    ) -> ReviewRoundRecord:
        """把一轮运行产出转成记录里的一轮, 轮次号与前后时间由调用方给出.

        工具事件先按 sk- 规则脱敏再保存: 事件里的参数与错误消息可能夹带密钥,
        而记录会被工作台读出来展示.
        """

        events = json.loads(redact_secret(json.dumps(outcome.tool_events, ensure_ascii=False)))
        return cls(
            number=number,
            started_at=started_at,
            finished_at=finished_at,
            model=model,
            kind=outcome.kind,
            text=outcome.text,
            model_requests=outcome.model_requests,
            tool_attempts=outcome.tool_attempts,
            elapsed_seconds=outcome.elapsed_seconds,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            tool_events=tuple(events),
            report=outcome.report,
        )


class ReviewRecord(BaseModel):
    """一条本地审查记录: 材料身份, 各轮结果, 反馈与失败 (方案 6.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    review_id: UUID
    owner_user_id: UUID
    retry_of_review_id: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    status: ReviewStatus
    request_text: str = Field(min_length=1)
    reference_date: date
    original_filename: str = Field(min_length=1)
    material_id: str = ""
    material_sha256: str = ""
    page_count: int = Field(default=0, ge=0)
    supplements: tuple[str, ...] = ()
    rounds: tuple[ReviewRoundRecord, ...] = ()
    feedback: ReviewFeedback | None = None
    failure: ReviewFailure | None = None

    def with_material(self, material: MaterialDocument) -> ReviewRecord:
        """登记材料层读出的稳定身份 (材料 ID, 指纹与页数), 只允许登记一次.

        指纹必须与建档时保存的字节一致: 记录不能声称自己是另一份材料.
        """

        if self.status is not ReviewStatus.RUNNING:
            raise ReviewStateError(f"状态 {self.status} 的记录不能再登记材料身份")
        if self.material_id:
            raise ReviewStateError("材料身份已经登记过, 不能在同一个 review_id 下换成另一份材料")
        if material.sha256 != self.material_sha256:
            raise ReviewStateError(
                f"材料字节与建档时不一致: 建档时 {self.material_sha256}, 这次读到 {material.sha256}"
            )
        return self.model_copy(
            update={
                "material_id": material.material_id,
                "material_sha256": material.sha256,
                "page_count": material.page_count,
            }
        )

    def with_round(
        self,
        round_record: ReviewRoundRecord,
        *,
        failure: ReviewFailure | None = None,
    ) -> ReviewRecord:
        """追加一轮, 并按这一轮的结果推进状态.

        只有 `running` 的记录能追加轮次: `question` 状态下还没补充, `completed` 与
        `failed` 已经是一次运行的结果, 都不该再长出第二轮.

        失败轮次必须同时给出 `failure`, 非失败轮次不能带它: 失败的工具事件 (页面要展开
        出错的那一步) 与稳定错误码要么一起落盘, 要么都不落; 记录里失败与报告不会同时
        存在.
        """

        if self.status is not ReviewStatus.RUNNING:
            raise ReviewStateError(f"状态 {self.status} 的记录不能再追加轮次")
        expected = len(self.rounds) + 1
        if round_record.number != expected:
            raise ReviewStateError(f"轮次号必须是 {expected}, 收到 {round_record.number}")
        if round_record.kind == "failed" and failure is None:
            raise ReviewStateError("失败轮次必须同时给出 failure (错误码, 用户提示与能否重跑)")
        if round_record.kind != "failed" and failure is not None:
            raise ReviewStateError("非失败轮次不能带 failure")
        updates: dict[str, Any] = {
            "rounds": (*self.rounds, round_record),
            "status": _STATUS_BY_KIND[round_record.kind],
        }
        if failure is not None:
            updates["failure"] = failure
        return self.model_copy(update=updates)

    def with_supplement(self, text: str) -> ReviewRecord:
        """登记一次用户补充: 只有追问后的记录能补充, 且只能补充一次."""

        if self.status is not ReviewStatus.QUESTION:
            raise ReviewStateError(f"状态 {self.status} 的记录不能补充: 只有追问后的记录能补充一次")
        if self.supplements:
            raise ReviewStateError("补充只能登记一次, 要改变意见请创建重查记录")
        if not text.strip():
            raise ReviewStateError("用户补充不能为空")
        return self.model_copy(
            update={"supplements": (*self.supplements, text), "status": ReviewStatus.RUNNING}
        )

    def with_feedback(
        self,
        *,
        decision: Literal["confirmed", "recheck_requested"],
        comment: str = "",
        now: datetime | None = None,
    ) -> ReviewRecord:
        """提交一次报告反馈: 只有完成的记录能提交, 且不能覆盖已有反馈.

        反馈只表示用户看过了这份报告: 它不改状态, 也不代表准入决定.
        """

        if self.status is not ReviewStatus.COMPLETED:
            raise ReviewStateError(f"状态 {self.status} 的记录不能提交反馈: 只有完成的报告能反馈")
        if self.feedback is not None:
            raise ReviewStateError("反馈已经提交过, 不能覆盖; 需要改变意见请创建重查记录")
        return self.model_copy(
            update={
                "feedback": ReviewFeedback(
                    decision=decision,
                    comment=comment,
                    created_at=now or datetime.now(UTC),
                )
            }
        )

    def with_failure(self, *, code: str, message: str, retryable: bool = False) -> ReviewRecord:
        """把失败写进记录: 只有还没结论的记录能失败, 失败与报告不能同时存在."""

        if self.status not in (ReviewStatus.RUNNING, ReviewStatus.QUESTION):
            raise ReviewStateError(f"状态 {self.status} 的记录不能改成失败")
        if self.failure is not None:
            raise ReviewStateError("这条记录已经记过一次失败")
        return self.model_copy(
            update={
                "failure": ReviewFailure(code=code, message=message, retryable=retryable),
                "status": ReviewStatus.FAILED,
            }
        )

    def available_actions(self) -> tuple[AllowedAction, ...]:
        """按当前状态给出可执行的动作, 供详情响应的 `allowed_actions` 字段使用.

        页面只显示这些按钮, 不自己推导规则 (方案第 7 节):

        - `supplement`: 追问后还没补充过, 可以补充一次 (一次补充的限制在状态机上);
        - `feedback`: 报告已完成且还没反馈, 可以确认或要求重查;
        - `rerun`: 可重试的失败, 或已要求重查的完成记录, 可以用原记录创建新的运行.
        """

        actions: list[AllowedAction] = []
        if self.status is ReviewStatus.QUESTION and not self.supplements:
            actions.append("supplement")
        if self.status is ReviewStatus.COMPLETED and self.feedback is None:
            actions.append("feedback")
        retryable_failure = self.failure is not None and self.failure.retryable
        recheck_requested = (
            self.feedback is not None and self.feedback.decision == "recheck_requested"
        )
        if retryable_failure or recheck_requested:
            actions.append("rerun")
        return tuple(actions)


class ReviewSummary(BaseModel):
    """列表用的摘要: 不带材料全文, 轮次与工具事件 (方案 7.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: UUID
    owner_user_id: UUID
    original_filename: str
    status: ReviewStatus
    created_at: AwareDatetime
    request_excerpt: str
    finding_count: int = Field(ge=0)
    retry_of_review_id: UUID | None = None

    @classmethod
    def from_record(cls, record: ReviewRecord) -> ReviewSummary:
        """从一条记录投影出摘要: 请求文本截断, 发现数取最后一轮的报告."""

        last_report = next(
            (item.report for item in reversed(record.rounds) if item.report is not None),
            None,
        )
        return cls(
            review_id=record.review_id,
            owner_user_id=record.owner_user_id,
            original_filename=record.original_filename,
            status=record.status,
            created_at=record.created_at,
            request_excerpt=_excerpt(record.request_text),
            finding_count=len(last_report.findings) if last_report is not None else 0,
            retry_of_review_id=record.retry_of_review_id,
        )


class LocalReviewStore:
    """本地审查记录仓库: 目录布局见模块文档, 所有操作都核对所有者."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._locks: dict[UUID, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def create(
        self,
        *,
        owner_user_id: UUID,
        request_text: str,
        reference_date: date,
        original_filename: str,
        material_bytes: bytes,
        retry_of_review_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ReviewRecord:
        """创建一条 running 记录并保存材料字节; 中途失败时不留下半份目录.

        材料文件固定命名 `material.pdf`, 上传文件名只写进记录作展示用. 材料的
        `material_id` 与页数要等材料层读过之后再调用 `with_material()` 登记.
        """

        review_id = uuid4()
        created_at = now or datetime.now(UTC)
        directory = self.root / str(review_id)
        directory.mkdir(parents=True)
        try:
            (directory / MATERIAL_FILENAME).write_bytes(material_bytes)
            record = ReviewRecord(
                review_id=review_id,
                owner_user_id=owner_user_id,
                retry_of_review_id=retry_of_review_id,
                created_at=created_at,
                updated_at=created_at,
                status=ReviewStatus.RUNNING,
                request_text=request_text,
                reference_date=reference_date,
                original_filename=original_filename,
                material_sha256=hashlib.sha256(material_bytes).hexdigest(),
            )
            self._write_record(directory, record)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def get_for_owner(self, review_id: str | UUID, *, owner_user_id: UUID) -> ReviewRecord:
        """读取一条属于该用户的记录: 别人的、不存在的、id 非法的都报同一种错."""

        return self._read_for_owner(review_id, owner_user_id=owner_user_id)

    def read_material_bytes(self, review_id: str | UUID, *, owner_user_id: UUID) -> bytes:
        """读取这条记录保存的原始材料字节, 供受保护的材料接口返回给页面."""

        path = self.material_path_for_owner(review_id, owner_user_id=owner_user_id)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ReviewStoreError(f"材料文件无法读取: {path}") from exc

    def material_path_for_owner(self, review_id: str | UUID, *, owner_user_id: UUID) -> Path:
        """给出这条记录保存的材料文件路径 (先核对所有者), 交给材料层去读它.

        路径只在这里拼: 调用方拿到的是仓库给的路径, 不能自己按 review_id 拼, 也就不可能
        绕开所有者检查读到别人的材料.
        """

        record = self._read_for_owner(review_id, owner_user_id=owner_user_id)
        path = self._directory_for(record.review_id) / MATERIAL_FILENAME
        if not path.is_file():
            raise ReviewStoreError(f"材料文件不存在: {path}")
        return path

    def mutate_for_owner(
        self,
        review_id: str | UUID,
        *,
        owner_user_id: UUID,
        mutate: Callable[[ReviewRecord], ReviewRecord],
    ) -> ReviewRecord:
        """在临界区内读取, 修改并写回一条记录, 返回写盘后的记录.

        `mutate` 在锁内被调用, 状态检查与写盘属于同一次进入临界区的过程: 两个补充
        或反馈请求不会各自"先检查再写", 所以不会互相覆盖. `mutate` 必须返回同一条
        记录的新版本——`review_id` 与 `owner_user_id` 不允许被改动.
        """

        parsed = _parse_review_id(review_id)
        with self._lock_for(parsed):
            current = self._read_for_owner(parsed, owner_user_id=owner_user_id)
            updated = mutate(current)
            if not isinstance(updated, ReviewRecord):
                raise ReviewStoreError("mutate 必须返回 ReviewRecord")
            if (
                updated.review_id != current.review_id
                or updated.owner_user_id != current.owner_user_id
            ):
                raise ReviewStoreError("记录的 review_id 与 owner_user_id 不能被修改")
            stamped = updated.model_copy(update={"updated_at": datetime.now(UTC)})
            self._write_record(self._directory_for(current.review_id), stamped)
            return stamped

    def list_for_owner(self, *, owner_user_id: UUID, limit: int = 20) -> list[ReviewSummary]:
        """列出该用户的记录摘要, 按创建时间倒序 (同一时刻按 review_id 稳定排序)."""

        if limit < 1:
            raise ReviewStoreError("limit 必须是正整数")
        summaries = [
            ReviewSummary.from_record(record)
            for record in self._read_all_records()
            if record.owner_user_id == owner_user_id
        ]
        summaries.sort(key=lambda item: (item.created_at, str(item.review_id)), reverse=True)
        return summaries[:limit]

    def _read_all_records(self) -> list[ReviewRecord]:
        """读回根目录下的全部记录; 只认带 record.json 的子目录."""

        if not self.root.is_dir():
            return []
        return [
            self._read_record(directory)
            for directory in sorted(self.root.iterdir())
            if directory.is_dir()
        ]

    def _read_for_owner(self, review_id: str | UUID, *, owner_user_id: UUID) -> ReviewRecord:
        """解析 id, 读盘并核对所有者; 任一环节不成立都报 ReviewNotFoundError."""

        record = self._read_record(self._directory_for(_parse_review_id(review_id)))
        if record.owner_user_id != owner_user_id:
            raise ReviewNotFoundError(f"审查记录不存在: {review_id}")
        return record

    def _read_record(self, directory: Path) -> ReviewRecord:
        """按固定文件名读一条记录; 文件缺失, 坏 JSON 与 Schema 不符分别报错."""

        path = directory / RECORD_FILENAME
        if not path.is_file():
            raise ReviewNotFoundError(f"审查记录不存在: {directory.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewStoreError(f"审查记录无法解析: {path}") from exc
        try:
            return ReviewRecord.model_validate(payload)
        except ValidationError as exc:
            raise ReviewStoreError(f"审查记录不符合当前 Schema: {path}") from exc

    def _write_record(self, directory: Path, record: ReviewRecord) -> None:
        """先写同目录临时文件, 再原子替换 record.json; 失败时清掉临时文件."""

        target = directory / RECORD_FILENAME
        temporary = directory / _TEMP_FILENAME
        raw = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2)
        try:
            temporary.write_text(redact_secret(raw), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _directory_for(self, review_id: UUID) -> Path:
        """记录目录只用 UUID 的规范形式拼出来: 目录名不来自外部文本."""

        return self.root / str(review_id)

    def _lock_for(self, review_id: UUID) -> threading.Lock:
        """取这条记录的串行锁: 同一进程内按 review_id 串行, 不同记录互不影响."""

        with self._locks_guard:
            lock = self._locks.get(review_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[review_id] = lock
            return lock


def _parse_review_id(review_id: str | UUID) -> UUID:
    """把外部传入的 review_id 解析成 UUID, 形状不对一律按"记录不存在"处理.

    路径遍历片段 ("../..") 与随机字符串都在这里被挡下; 之后拼路径只用 UUID 的
    规范字符串形式, 所以外部文本永远进不了文件路径.
    """

    if isinstance(review_id, UUID):
        return review_id
    try:
        return UUID(str(review_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReviewNotFoundError(f"审查记录不存在: {review_id!r}") from exc


def _excerpt(text: str) -> str:
    """把请求文本截到固定长度, 截断时留一个省略号让页面知道这里被截过."""

    if len(text) <= REQUEST_EXCERPT_CHARS:
        return text
    return text[:REQUEST_EXCERPT_CHARS] + "…"


__all__ = [
    "MATERIAL_FILENAME",
    "RECORD_FILENAME",
    "REQUEST_EXCERPT_CHARS",
    "SCHEMA_VERSION",
    "AllowedAction",
    "LocalReviewStore",
    "ReviewFailure",
    "ReviewFeedback",
    "ReviewNotFoundError",
    "ReviewRecord",
    "ReviewRoundRecord",
    "ReviewStateError",
    "ReviewStatus",
    "ReviewStoreError",
    "ReviewSummary",
]
