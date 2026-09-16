"""本地审查记录: Schema, 原子读写, 所有权与状态转换.

M4 方案 4.3 与第 6 节: 审查记录是**本地演示证据**, 不是业务案件——它用独立的
`review_id` 与 `owner_user_id`, 不引用 `admission_cases`, 也不调人工准入决定接口.
本文件钉住的契约:

- 目录固定 `<root>/<review_id>/{material.pdf, record.json}`; `review_id` 由服务端生成
  UUID, 外部传入的路径片段一律按"记录不存在"处理, 上传文件名只作展示字段;
- `record.json` 带显式 `schema_version`, 时间必须带时区; 写盘先写同目录临时文件再
  原子替换, 替换失败时旧记录不变且不留临时文件;
- 所有读写都核对 `owner_user_id`: 别人的记录与不存在的记录报同一种错误;
- 同一 `review_id` 的"检查状态 + 写盘"在同一次 `mutate_for_owner()` 临界区内,
  并发修改不会互相覆盖;
- 状态机: `running` -> `completed` / `question` / `failed`, `question` 只能补充一次,
  只有 `completed` 能提交反馈且反馈不可覆盖.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from vendorguard import review_records
from vendorguard.agent import AgentRunOutcome
from vendorguard.agent_report import Finding, ReviewReport
from vendorguard.materials import MaterialDocument, read_text_pdf
from vendorguard.review_records import (
    MATERIAL_FILENAME,
    RECORD_FILENAME,
    REQUEST_EXCERPT_CHARS,
    LocalReviewStore,
    ReviewNotFoundError,
    ReviewRecord,
    ReviewRoundRecord,
    ReviewStateError,
    ReviewStatus,
    ReviewStoreError,
)

from .test_agent import MATERIALS_DIR, make_outcome

OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER_OWNER = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
DEFAULT_REQUEST = "请检查这家供应商的材料是否满足准入要求."
SUPPLEMENT = "补充说明: 营业执照有效期至 2030年01月31日"
QUESTION_TEXT = "请提供营业执照有效期截止日?"
NODE_KEY = "demo_supplier_admission_policy_v2_section_4"


@pytest.fixture
def store(tmp_path: Path) -> LocalReviewStore:
    """一份空仓库: 所有落盘都发生在临时目录里, 不碰工作区的 var/reviews."""

    return LocalReviewStore(tmp_path / "reviews")


def demo_bytes(name: str = "license_complete.pdf") -> bytes:
    """取一份自制样例的原始字节, 当作"用户上传的材料"."""

    return (MATERIALS_DIR / name).read_bytes()


def read_saved_material(store: LocalReviewStore, record: ReviewRecord) -> MaterialDocument:
    """按固定布局读回本次保存的那份 material.pdf.

    生产路径也是这么读的: 上传字节先落成 `material.pdf`, 再由材料层读取,
    所以 `material_id` 是固定文件名的主干, 与上传文件名无关.
    """

    return read_text_pdf(store.root / str(record.review_id) / MATERIAL_FILENAME)


def create_record(
    store: LocalReviewStore,
    *,
    owner: UUID = OWNER,
    now: datetime = NOW,
    request_text: str = DEFAULT_REQUEST,
    retry_of_review_id: UUID | None = None,
) -> ReviewRecord:
    """建档: 只保存材料字节与请求, 还没读材料层, 状态是 running."""

    return store.create(
        owner_user_id=owner,
        request_text=request_text,
        reference_date=date(2026, 9, 1),
        original_filename="license_complete.pdf",
        material_bytes=demo_bytes(),
        retry_of_review_id=retry_of_review_id,
        now=now,
    )


def with_material_record(store: LocalReviewStore, **kwargs) -> ReviewRecord:
    """建档并登记材料身份 (材料层读出的 material_id / 指纹 / 页数)."""

    record = create_record(store, **kwargs)
    document = read_saved_material(store, record)
    return store.mutate_for_owner(
        record.review_id,
        owner_user_id=record.owner_user_id,
        mutate=lambda current: current.with_material(document),
    )


def append_round(store: LocalReviewStore, record: ReviewRecord, round_record) -> ReviewRecord:
    """在记录上追加一轮, 返回更新后的记录."""

    return store.mutate_for_owner(
        record.review_id,
        owner_user_id=record.owner_user_id,
        mutate=lambda current: current.with_round(round_record),
    )


def round_from(
    outcome: AgentRunOutcome,
    number: int,
    *,
    model: str = "qwen3.8-max",
    started_at: datetime = NOW,
) -> ReviewRoundRecord:
    """把一份运行产出转成记录里的一轮, 时间戳固定便于断言."""

    return ReviewRoundRecord.from_outcome(
        outcome,
        number=number,
        model=model,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=3),
    )


def question_outcome() -> AgentRunOutcome:
    """一轮追问产出 (ask_user 成功后的形状)."""

    return make_outcome(QUESTION_TEXT, kind="question")


def answer_outcome(*, citations: tuple[str, ...] = ()) -> AgentRunOutcome:
    """一轮带结构化报告的产出 (submit_report 成功后的形状)."""

    report = ReviewReport(
        material_id="material",
        findings=(
            Finding(
                summary="营业执照声明有效期至 2027-08-31, 按参考日期判定为有效.",
                fact_fields=("business_license_valid_until",),
                material_sources=("material@page:1",),
                policy_citations=citations,
            ),
        ),
    )
    return make_outcome("已交付结构化报告, 共 1 条发现", kind="answer").model_copy(
        update={"report": report}
    )


def question_record(store: LocalReviewStore, **kwargs) -> ReviewRecord:
    """走到"等用户补充"状态的一条记录."""

    record = with_material_record(store, **kwargs)
    return append_round(store, record, round_from(question_outcome(), 1))


def completed_record(store: LocalReviewStore, **kwargs) -> ReviewRecord:
    """走到"报告已完成"状态的一条记录."""

    record = with_material_record(store, **kwargs)
    return append_round(store, record, round_from(answer_outcome(), 1))


def failed_record(store: LocalReviewStore, **kwargs) -> ReviewRecord:
    """走到"失败"状态的一条记录 (模型请求失败的形状)."""

    record = with_material_record(store, **kwargs)
    return store.mutate_for_owner(
        record.review_id,
        owner_user_id=record.owner_user_id,
        mutate=lambda current: current.with_failure(
            code="model_request_failed",
            message="模型请求失败: Connection error.",
            retryable=True,
        ),
    )


def test_create_writes_the_fixed_layout_and_every_field_reads_back(store) -> None:
    """建档: 布局固定为 <root>/<review_id>/{material.pdf, record.json}, 字段逐项可读回."""

    payload = demo_bytes()
    record = store.create(
        owner_user_id=OWNER,
        request_text=DEFAULT_REQUEST,
        reference_date=date(2026, 9, 1),
        original_filename="license_complete.pdf",
        material_bytes=payload,
        now=NOW,
    )

    directory = store.root / str(record.review_id)
    assert sorted(item.name for item in directory.iterdir()) == [
        MATERIAL_FILENAME,
        RECORD_FILENAME,
    ]
    # review_id 由服务端生成, 是规范形式的 UUID; 材料文件名固定, 与上传文件名无关
    assert UUID(str(record.review_id))
    assert (directory / MATERIAL_FILENAME).read_bytes() == payload
    assert record.material_sha256 == hashlib.sha256(payload).hexdigest()

    stored = json.loads((directory / RECORD_FILENAME).read_text(encoding="utf-8"))
    assert stored["schema_version"] == "1.0"
    assert stored["status"] == "running"
    assert stored["owner_user_id"] == str(OWNER)
    assert datetime.fromisoformat(stored["created_at"]) == NOW

    reloaded = store.get_for_owner(record.review_id, owner_user_id=OWNER)
    assert reloaded == record
    assert reloaded.created_at == NOW
    assert reloaded.updated_at == NOW
    assert reloaded.status is ReviewStatus.RUNNING
    assert reloaded.request_text == DEFAULT_REQUEST
    assert reloaded.reference_date == date(2026, 9, 1)
    assert reloaded.original_filename == "license_complete.pdf"
    assert reloaded.material_id == ""
    assert reloaded.page_count == 0
    assert reloaded.supplements == ()
    assert reloaded.rounds == ()
    assert reloaded.feedback is None
    assert reloaded.failure is None
    assert reloaded.retry_of_review_id is None


def test_upload_filename_is_display_only(store) -> None:
    """上传文件名只作展示: 带路径片段的名字既不拼路径, 也不影响落盘位置."""

    record = store.create(
        owner_user_id=OWNER,
        request_text=DEFAULT_REQUEST,
        reference_date=date(2026, 9, 1),
        original_filename="../../etc/passwd.pdf",
        material_bytes=demo_bytes(),
        now=NOW,
    )

    assert record.original_filename == "../../etc/passwd.pdf"
    assert [item.name for item in store.root.iterdir()] == [str(record.review_id)]
    assert sorted(item.name for item in (store.root / str(record.review_id)).iterdir()) == [
        MATERIAL_FILENAME,
        RECORD_FILENAME,
    ]


def test_material_identity_comes_from_the_saved_file(store) -> None:
    """材料身份按保存的那份 material.pdf 读出, 字节与上传时完全一致."""

    payload = demo_bytes()
    record = with_material_record(store)

    assert record.material_id == "material"
    assert record.material_sha256 == hashlib.sha256(payload).hexdigest()
    assert record.page_count == read_saved_material(store, record).page_count
    assert store.read_material_bytes(record.review_id, owner_user_id=OWNER) == payload
    assert store.get_for_owner(record.review_id, owner_user_id=OWNER) == record


def test_material_identity_is_registered_only_once_and_must_match(store) -> None:
    """材料身份只登记一次, 且只能登记与建档字节一致的那一份."""

    record = with_material_record(store)
    other_document = read_text_pdf(MATERIALS_DIR / "license_missing_date.pdf")
    raw = create_record(store, now=NOW + timedelta(minutes=1))

    with pytest.raises(ReviewStateError):
        # 指纹不符: 记录不能说自己是另一份材料
        record.with_material(other_document)
    with pytest.raises(ReviewStateError):
        # 已经登记过材料身份
        record.with_material(read_saved_material(store, record))
    with pytest.raises(ReviewStateError):
        # 状态不是 running: 一轮已经跑完的记录不能再改材料身份
        question_record(store, now=NOW + timedelta(minutes=2)).with_material(other_document)
    assert raw.material_id == ""


@pytest.mark.parametrize("bad_id", ["../..", "..\\..", "/etc/passwd", "not-a-uuid", "", "1"])
def test_review_id_must_be_a_uuid(store, bad_id: str) -> None:
    """外部传入的 review_id 只按 UUID 解析: 路径片段一律按"记录不存在"处理."""

    record = create_record(store)

    with pytest.raises(ReviewNotFoundError):
        store.get_for_owner(bad_id, owner_user_id=OWNER)
    with pytest.raises(ReviewNotFoundError):
        store.read_material_bytes(bad_id, owner_user_id=OWNER)
    with pytest.raises(ReviewNotFoundError):
        store.mutate_for_owner(bad_id, owner_user_id=OWNER, mutate=lambda current: current)
    # 非法 id 没有在磁盘上留下任何东西
    assert [item.name for item in store.root.iterdir()] == [str(record.review_id)]


def test_records_of_other_users_look_missing(store) -> None:
    """别人的记录与不存在的记录报同一种错误: 接口据此统一返回 404, 不泄露存在性."""

    record = create_record(store, owner=OWNER)

    with pytest.raises(ReviewNotFoundError):
        store.get_for_owner(record.review_id, owner_user_id=OTHER_OWNER)
    with pytest.raises(ReviewNotFoundError):
        store.read_material_bytes(record.review_id, owner_user_id=OTHER_OWNER)
    with pytest.raises(ReviewNotFoundError):
        store.mutate_for_owner(
            record.review_id,
            owner_user_id=OTHER_OWNER,
            mutate=lambda current: current.with_supplement(SUPPLEMENT),
        )
    with pytest.raises(ReviewNotFoundError):
        store.mutate_for_owner(
            record.review_id,
            owner_user_id=OTHER_OWNER,
            mutate=lambda current: current.with_failure(code="x", message="y"),
        )
    with pytest.raises(ReviewNotFoundError):
        store.get_for_owner(uuid4(), owner_user_id=OWNER)

    assert store.get_for_owner(record.review_id, owner_user_id=OWNER) == record


def test_answer_round_is_persisted_with_its_report(store) -> None:
    """一轮 answer 结果落盘: 报告与用量字段都能读回, 状态推进为 completed."""

    record = with_material_record(store)
    completed = append_round(store, record, round_from(answer_outcome(citations=(NODE_KEY,)), 1))

    assert completed.status is ReviewStatus.COMPLETED
    assert len(completed.rounds) == 1
    stored_round = completed.rounds[0]
    assert stored_round.number == 1
    assert stored_round.kind == "answer"
    assert stored_round.model == "qwen3.8-max"
    assert stored_round.started_at == NOW
    assert stored_round.finished_at == NOW + timedelta(seconds=3)
    assert stored_round.model_requests == 3
    assert stored_round.tool_attempts == 2
    assert stored_round.elapsed_seconds == pytest.approx(1.25)
    assert stored_round.prompt_tokens == 300
    assert stored_round.completion_tokens == 50
    assert stored_round.report is not None
    assert stored_round.report.findings[0].policy_citations == (NODE_KEY,)
    assert [event["name"] for event in stored_round.tool_events] == [
        "read_material",
        "check_materials",
    ]

    # 重启进程后重新读取: 换一个 store 实例, 报告还在
    reopened = LocalReviewStore(store.root)
    reloaded = reopened.get_for_owner(record.review_id, owner_user_id=OWNER)
    assert reloaded == completed
    assert reloaded.rounds[0].report is not None


def test_failure_is_persisted_and_a_finished_record_cannot_fail(store) -> None:
    """失败按稳定错误码落盘; 已有结论的记录不能被改成失败."""

    failed = failed_record(store)

    assert failed.status is ReviewStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.code == "model_request_failed"
    assert failed.failure.retryable is True
    assert failed.rounds == ()

    with pytest.raises(ReviewStateError):
        completed_record(store, now=NOW + timedelta(minutes=1)).with_failure(
            code="model_request_failed", message="模型请求失败", retryable=True
        )


def test_supplement_is_allowed_once_and_only_from_question(store) -> None:
    """补充只在 question 状态登记一次, 登记后回到 running; 空文本不算来源."""

    question = question_record(store)

    running = question.with_supplement(SUPPLEMENT)

    assert running.status is ReviewStatus.RUNNING
    assert running.supplements == (SUPPLEMENT,)
    assert running.rounds == question.rounds
    # 已经离开 question 状态的记录不能再补充: 一次补充的限制在状态机上
    with pytest.raises(ReviewStateError):
        running.with_supplement(SUPPLEMENT)
    with pytest.raises(ReviewStateError):
        completed_record(store, now=NOW + timedelta(minutes=1)).with_supplement(SUPPLEMENT)
    with pytest.raises(ReviewStateError):
        question.with_supplement("   ")


def test_feedback_requires_a_completed_record_and_cannot_be_overwritten(store) -> None:
    """反馈只在 completed 提交一次; 想改变意见要创建重查记录, 不能覆盖旧反馈."""

    question = question_record(store)
    with pytest.raises(ReviewStateError):
        question.with_feedback(decision="confirmed", now=NOW + timedelta(minutes=1))
    with pytest.raises(ValidationError):
        completed_record(store).with_feedback(decision="approved", now=NOW)  # type: ignore[arg-type]

    completed = completed_record(store, now=NOW + timedelta(minutes=2))
    reviewed = store.mutate_for_owner(
        completed.review_id,
        owner_user_id=OWNER,
        mutate=lambda current: current.with_feedback(
            decision="confirmed",
            comment="来源与报告内容已核对",
            now=NOW + timedelta(minutes=3),
        ),
    )

    assert reviewed.status is ReviewStatus.COMPLETED
    assert reviewed.feedback is not None
    assert reviewed.feedback.decision == "confirmed"
    assert reviewed.feedback.comment == "来源与报告内容已核对"
    assert reviewed.feedback.created_at == NOW + timedelta(minutes=3)

    with pytest.raises(ReviewStateError):
        reviewed.with_feedback(decision="recheck_requested", now=NOW + timedelta(minutes=4))

    # 落盘后的记录同样拒绝第二次反馈: 临界区里抛错, 已保存的反馈一点没变
    with pytest.raises(ReviewStateError):
        store.mutate_for_owner(
            completed.review_id,
            owner_user_id=OWNER,
            mutate=lambda current: current.with_feedback(
                decision="recheck_requested",
                comment="想改口径",
                now=NOW + timedelta(minutes=5),
            ),
        )
    assert store.get_for_owner(completed.review_id, owner_user_id=OWNER) == reviewed


def test_concurrent_mutations_do_not_lose_updates(store) -> None:
    """同一 review_id 的读改写在同一个临界区内: 并发追加不会互相覆盖."""

    record = with_material_record(store)
    markers = [f"[线程{index}]" for index in range(8)]
    barrier = threading.Barrier(len(markers))
    errors: list[BaseException] = []

    def worker(marker: str) -> None:
        def mutate(current: ReviewRecord) -> ReviewRecord:
            # 放大"读-改-写"窗口: 没有串行化时, 后写的线程会盖掉先写的
            time.sleep(0.02)
            return current.model_copy(update={"request_text": current.request_text + marker})

        try:
            barrier.wait(timeout=5)
            store.mutate_for_owner(record.review_id, owner_user_id=OWNER, mutate=mutate)
        except BaseException as exc:  # 线程里的异常要留给断言看, 不能吞
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(marker,)) for marker in markers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    final = store.get_for_owner(record.review_id, owner_user_id=OWNER)
    for marker in markers:
        assert marker in final.request_text


def test_failed_replace_keeps_the_previous_record_and_cleans_up(store, monkeypatch) -> None:
    """原子替换失败时: 旧记录一点没变, 目录里不留临时文件."""

    record = question_record(store)

    def broken_replace(source, target) -> None:
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(review_records.os, "replace", broken_replace)

    with pytest.raises(OSError):
        store.mutate_for_owner(
            record.review_id,
            owner_user_id=OWNER,
            mutate=lambda current: current.with_supplement(SUPPLEMENT),
        )

    reloaded = store.get_for_owner(record.review_id, owner_user_id=OWNER)
    assert reloaded == record
    assert sorted(item.name for item in (store.root / str(record.review_id)).iterdir()) == [
        MATERIAL_FILENAME,
        RECORD_FILENAME,
    ]


def test_record_file_requires_schema_version_and_timezone(store) -> None:
    """record.json 必须带显式 schema_version, 时间必须带时区; 不符时明确报错."""

    record = create_record(store)
    path = store.root / str(record.review_id) / RECORD_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))

    payload["schema_version"] = "2.0"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReviewStoreError, match="Schema"):
        store.get_for_owner(record.review_id, owner_user_id=OWNER)

    payload["schema_version"] = "1.0"
    payload["updated_at"] = "2026-09-15T12:00:00"  # 没有时区
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReviewStoreError, match="Schema"):
        store.get_for_owner(record.review_id, owner_user_id=OWNER)

    path.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(ReviewStoreError):
        store.get_for_owner(record.review_id, owner_user_id=OWNER)


def test_create_rejects_naive_time_and_leaves_nothing_behind(store) -> None:
    """建档时间必须带时区; 建档中途失败时不留下半份目录."""

    with pytest.raises(ValidationError):
        store.create(
            owner_user_id=OWNER,
            request_text=DEFAULT_REQUEST,
            reference_date=date(2026, 9, 1),
            original_filename="license_complete.pdf",
            material_bytes=demo_bytes(),
            now=datetime(2026, 9, 15, 12, 0),
        )

    assert not store.root.exists() or list(store.root.iterdir()) == []


def test_tool_events_are_redacted_before_saving(store) -> None:
    """工具事件落盘前执行 sk- 脱敏兜底: 密钥样式不会进记录文件."""

    secret = "sk-abcdef1234567890"
    outcome = make_outcome("模型请求失败: Connection error.", kind="failed").model_copy(
        update={
            "tool_events": [
                {
                    "tool_call_id": "call_1",
                    "name": "check_materials",
                    "status": "error",
                    "detail": f"模型请求失败: 401 unauthorized {secret}",
                    "arguments": None,
                }
            ]
        }
    )
    record = with_material_record(store)

    saved = append_round(store, record, round_from(outcome, 1))

    assert saved.rounds[0].tool_events[0]["detail"].endswith("sk-***")
    assert secret not in json.dumps(saved.model_dump(mode="json"), ensure_ascii=False)
    raw = (store.root / str(record.review_id) / RECORD_FILENAME).read_text(encoding="utf-8")
    assert secret not in raw
    assert "sk-***" in raw


def test_list_for_owner_returns_own_records_newest_first(store) -> None:
    """列表只含自己的记录, 按创建时间倒序, 摘要不带工具事件与报告全文."""

    long_request = "请检查这家供应商的材料" + "并逐条说明依据" * 20
    other = create_record(store, owner=OTHER_OWNER, now=NOW + timedelta(hours=1))
    oldest = create_record(store, now=NOW)
    middle = create_record(store, now=NOW + timedelta(minutes=1), request_text=long_request)
    newest = completed_record(store, now=NOW + timedelta(minutes=2))

    summaries = store.list_for_owner(owner_user_id=OWNER)

    assert [item.review_id for item in summaries] == [
        newest.review_id,
        middle.review_id,
        oldest.review_id,
    ]
    assert other.review_id not in [item.review_id for item in summaries]
    assert summaries[0].status is ReviewStatus.COMPLETED
    assert summaries[0].finding_count == 1
    assert summaries[0].created_at == NOW + timedelta(minutes=2)
    assert summaries[0].original_filename == "license_complete.pdf"
    assert summaries[0].request_excerpt == DEFAULT_REQUEST
    excerpt = next(item for item in summaries if item.review_id == middle.review_id).request_excerpt
    assert len(excerpt) <= REQUEST_EXCERPT_CHARS + 1
    assert long_request.startswith(excerpt.rstrip("…"))
    # 摘要字段表固定: 不带材料全文, 轮次与工具事件
    assert set(summaries[0].model_dump()) == {
        "review_id",
        "owner_user_id",
        "original_filename",
        "status",
        "created_at",
        "request_excerpt",
        "finding_count",
        "retry_of_review_id",
    }

    assert [item.review_id for item in store.list_for_owner(owner_user_id=OWNER, limit=1)] == [
        newest.review_id
    ]
    with pytest.raises(ReviewStoreError):
        store.list_for_owner(owner_user_id=OWNER, limit=0)


def test_retry_creates_a_new_record_and_leaves_the_old_one_untouched(store) -> None:
    """重跑创建新记录并留下关联; 旧记录的内容与状态一点不变."""

    source = failed_record(store)

    retry = create_record(
        store,
        now=NOW + timedelta(minutes=1),
        retry_of_review_id=source.review_id,
    )

    assert retry.review_id != source.review_id
    assert retry.retry_of_review_id == source.review_id
    assert retry.status is ReviewStatus.RUNNING
    assert retry.material_sha256 == source.material_sha256
    assert store.get_for_owner(source.review_id, owner_user_id=OWNER) == source
    assert source.failure is not None


def test_a_full_local_review_survives_a_restart(store) -> None:
    """一条完整本地审查: 建档 -> 追问 -> 补充 -> 报告, 换一个 store 实例仍读到两轮.

    这是 M4-2 的完成判据在测试里的形状: 不用 FastAPI, 只靠本地记录就能"存一轮、
    重启后读回来".
    """

    record = with_material_record(store)
    waiting = append_round(store, record, round_from(question_outcome(), 1))
    running = store.mutate_for_owner(
        waiting.review_id,
        owner_user_id=OWNER,
        mutate=lambda current: current.with_supplement(SUPPLEMENT),
    )
    answered = append_round(store, running, round_from(answer_outcome(citations=(NODE_KEY,)), 2))

    assert answered.status is ReviewStatus.COMPLETED
    assert answered.material_id == "material"
    assert answered.supplements == (SUPPLEMENT,)

    reopened = LocalReviewStore(store.root)
    reloaded = reopened.get_for_owner(record.review_id, owner_user_id=OWNER)

    assert reloaded == answered
    assert [item.number for item in reloaded.rounds] == [1, 2]
    assert reloaded.rounds[0].kind == "question"
    assert reloaded.rounds[0].text == QUESTION_TEXT
    assert reloaded.rounds[1].report is not None
    assert reloaded.rounds[1].report.findings[0].policy_citations == (NODE_KEY,)
    # 第二轮只带自己的引用: 第一轮的追问没有因为第二轮而被改写
    assert reloaded.rounds[0].report is None


def test_records_layer_has_no_database_dependency() -> None:
    """记录层不接数据库与业务案件: 本地文件是唯一的持久化 (M4 方案 4.3)."""

    source = Path(inspect.getsourcefile(review_records)).read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "vendorguard.database" not in source
    assert "vendorguard.admission" not in source
