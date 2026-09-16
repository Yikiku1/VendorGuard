"""审查工作台 HTTP 接口: 认证, 上传限制, 记录视图与统一错误体.

M4 方案第 7 节. 这些用例**不连数据库也不打真实模型**: 认证用 `get_current_user` 的
依赖覆盖换成固定用户, `ReviewRuntime` 与 `LocalReviewStore` 换成替身, 应用层那一轮用
`review_routes.run_review_round` 的替身直接返回 `AgentRunOutcome`. 真实登录与真实模型
属于 M4-7 的端到端验收, 不在本文件里假装跑过.

覆盖的契约: 未认证 401 且不建档; 上传成功得到自己的完整记录 (报告与工具事件都在);
扫描件形成 failed 记录且**没有请求模型**; 超过 10 MB 在模型之前拒绝; 表单参数错误
在建档前拒绝; 跨用户统一 404; 列表只给摘要; 材料接口字节与上传一致; 错误体统一.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from vendorguard import review_routes
from vendorguard.agent import AgentRunOutcome
from vendorguard.agent_report import Finding, ReviewReport
from vendorguard.app import create_app
from vendorguard.dependencies import get_current_user
from vendorguard.materials import read_text_pdf
from vendorguard.review_application import ReviewCommand
from vendorguard.review_records import (
    MATERIAL_FILENAME,
    LocalReviewStore,
    ReviewFailure,
    ReviewRoundRecord,
)
from vendorguard.review_routes import MAX_UPLOAD_BYTES, get_review_runtime, get_review_store
from vendorguard.security import UserRole

OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER_OWNER = UUID("22222222-2222-4222-8222-222222222222")
MATERIALS_DIR = Path(__file__).resolve().parents[2] / "data" / "demo" / "materials"
REFERENCE_DATE = "2026-09-01"
REQUEST_TEXT = "请检查这家供应商的材料是否满足准入要求."
NODE_KEY = "demo_supplier_admission_policy_v2_section_4"


class ModelDouble:
    """应用层替身: 记录是否被调用, 并按剧本返回一轮产出."""

    def __init__(self, outcome: AgentRunOutcome | None = None) -> None:
        self.calls: list[ReviewCommand] = []
        self._outcome = outcome

    def __call__(self, command: ReviewCommand, *, runtime: object) -> AgentRunOutcome:
        self.calls.append(command)
        assert self._outcome is not None, "这次调用没有准备剧本: 接口不该请求模型"
        return self._outcome


def make_outcome(text: str, *, kind: str, report: ReviewReport | None = None) -> AgentRunOutcome:
    """造一轮运行产出: 字段与 Agent 循环返回的形状一致."""

    return AgentRunOutcome(
        kind=kind,  # type: ignore[arg-type]
        text=text,
        model_requests=4,
        tool_attempts=4,
        elapsed_seconds=9.5,
        prompt_tokens=800,
        completion_tokens=120,
        tool_events=[
            {
                "tool_call_id": "call_read_1",
                "name": "read_material",
                "status": "ok",
                "detail": {"material_id": "material", "page_count": 1},
                "arguments": '{"material_id": "material"}',
            }
        ],
        user_request=REQUEST_TEXT,
        report=report,
    )


def answer_outcome(*, citations: tuple[str, ...] = (NODE_KEY,)) -> AgentRunOutcome:
    """一轮带结构化报告的产出 (submit_report 成功后的形状)."""

    report = ReviewReport(
        material_id=MATERIAL_FILENAME.rsplit(".", 1)[0],
        findings=(
            Finding(
                summary="营业执照声明有效期至 2027-08-31, 按参考日期判定为有效.",
                fact_fields=("business_license_valid_until",),
                material_sources=("material@page:1",),
                policy_citations=citations,
            ),
        ),
    )
    return make_outcome("已交付结构化报告, 共 1 条发现", kind="answer", report=report)


class ReviewApiFixture(SimpleNamespace):
    """一次接口用例的三件套: 客户端, 记录仓库与模型替身."""

    client: TestClient
    store: LocalReviewStore
    model: ModelDouble
    runtime: SimpleNamespace

    def create(
        self,
        *,
        material_name: str = "license_complete.pdf",
        material_bytes: bytes | None = None,
        filename: str | None = None,
        **form: object,
    ):
        """按接口契约提交一次审查 (multipart/form-data)."""

        payload = {
            "request_text": REQUEST_TEXT,
            "reference_date": REFERENCE_DATE,
        }
        payload.update(form)
        body = (
            material_bytes
            if material_bytes is not None
            else (MATERIALS_DIR / material_name).read_bytes()
        )
        return self.client.post(
            "/api/reviews",
            data=payload,
            files={
                "material": (
                    filename or material_name,
                    body,
                    "application/pdf",
                )
            },
        )


def api_client(
    tmp_path: Path,
    *,
    owner: UUID = OWNER,
    outcome: AgentRunOutcome | None = None,
    monkeypatch: MonkeyPatch | None = None,
    authenticated: bool = True,
) -> ReviewApiFixture:
    """造一个只走接口层的应用: 认证, runtime 与 store 都换成替身.

    认证用依赖覆盖而不是真实登录: 真实登录要连数据库 (M4-7 的端到端验收再跑它).
    `authenticated=False` 时保留真实的 `get_current_user`, 用来验证未认证分支.
    应用启动时的那次审查依赖装载由 tests/integration/conftest.py 统一替换.
    """

    store = LocalReviewStore(tmp_path / "reviews")
    runtime = SimpleNamespace(model_name="fake-model", client=None, policy=None, policy_index=None)
    model = ModelDouble(outcome)

    app = create_app()
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=owner,
            username="demo.specialist",
            role=UserRole.PROCUREMENT_SPECIALIST,
            is_active=True,
        )
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_review_runtime] = lambda: runtime

    if monkeypatch is not None:
        monkeypatch.setattr(review_routes, "run_review_round", model)

    return ReviewApiFixture(
        client=TestClient(app, raise_server_exceptions=False),
        store=store,
        model=model,
        runtime=runtime,
    )


def record_directories(fixture: ReviewApiFixture) -> list[Path]:
    """本地记录目录下的条目名 (用例用它断言"有没有建档")."""

    root = fixture.store.root
    return [] if not root.is_dir() else sorted(item.name for item in root.iterdir())


def test_reviews_require_authentication(tmp_path: Path) -> None:
    """未认证: 401 + 统一错误体, 不创建任何记录."""

    fixture = api_client(tmp_path, authenticated=False)

    with fixture.client as client:
        listed = client.get("/api/reviews")
        created = client.post(
            "/api/reviews",
            data={"request_text": REQUEST_TEXT, "reference_date": REFERENCE_DATE},
            files={"material": ("license_complete.pdf", b"%PDF-1.4", "application/pdf")},
        )
        material = client.get(f"/api/reviews/{OWNER}/material")

    for response in (listed, created, material):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert response.json()["error"]["retryable"] is False
    assert created.headers["www-authenticate"] == "Bearer"
    assert record_directories(fixture) == []


def test_create_review_returns_the_full_record(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """上传文本 PDF: 201 与完整记录视图, 报告与工具事件都能在响应里读到."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)

    with fixture.client as client:
        response = fixture.create()

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["original_filename"] == "license_complete.pdf"
    assert body["material_id"] == "material"
    assert body["page_count"] == 1
    assert body["reference_date"] == REFERENCE_DATE
    assert [item["name"] for item in body["rounds"][0]["tool_events"]] == ["read_material"]
    finding = body["rounds"][0]["report"]["findings"][0]
    assert finding["policy_citations"] == [NODE_KEY]
    assert finding["material_sources"] == ["material@page:1"]
    assert body["allowed_actions"] == ["feedback"]
    assert body["failure"] is None
    assert body["feedback"] is None
    assert len(fixture.model.calls) == 1

    # 响应就是落盘的那条记录: 再 GET 一次得到同一份报告与工具事件
    with fixture.client as client:
        reloaded = client.get(f"/api/reviews/{body['review_id']}")

    assert reloaded.status_code == 200
    assert reloaded.json() == body
    stored = fixture.store.get_for_owner(body["review_id"], owner_user_id=OWNER)
    assert stored.status.value == "completed"
    assert stored.rounds[0].report is not None


def test_scanned_material_leaves_a_failed_record_without_calling_the_model(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """扫描件: 留下 failed 记录, 不请求模型, 也不能用原记录重跑."""

    fixture = api_client(tmp_path, monkeypatch=monkeypatch)

    with fixture.client:
        response = fixture.create(material_name="license_scanned.pdf")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["rounds"] == []
    assert body["failure"]["code"] == "material_scanned_unsupported"
    assert body["failure"]["retryable"] is False
    assert "扫描件" in body["failure"]["message"]
    assert body["allowed_actions"] == []
    assert fixture.model.calls == []


def test_upload_over_the_limit_is_rejected_before_creating_a_record(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """超过 10 MB 的上传在建档前拒绝, 更不会请求模型."""

    fixture = api_client(tmp_path, monkeypatch=monkeypatch)
    oversized = b"%PDF-1.4" + b"x" * MAX_UPLOAD_BYTES

    with fixture.client:
        response = fixture.create(material_bytes=oversized, filename="huge.pdf")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert response.json()["error"]["retryable"] is False
    assert record_directories(fixture) == []
    assert fixture.model.calls == []


@pytest.mark.parametrize(
    "form",
    [
        {"request_text": "   "},
        {"request_text": "请检查" * 400},
        {"reference_date": "2026/09/01"},
        {"reference_date": "not-a-date"},
    ],
)
def test_invalid_form_values_are_rejected_before_creating_a_record(
    tmp_path: Path, monkeypatch: MonkeyPatch, form: dict
) -> None:
    """表单参数错误在建档前拒绝: 400 + invalid_request, 不留下半个记录."""

    fixture = api_client(tmp_path, monkeypatch=monkeypatch)

    with fixture.client:
        response = fixture.create(**form)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert record_directories(fixture) == []
    assert fixture.model.calls == []


def test_non_multipart_body_gets_the_unified_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """用 JSON 调用上传接口也得到统一错误体, 不是 FastAPI 默认的 detail 列表."""

    fixture = api_client(tmp_path, monkeypatch=monkeypatch)

    with fixture.client as client:
        response = client.post("/api/reviews", json={"request_text": REQUEST_TEXT})

    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["retryable"] is False
    assert body["error"]["message"]
    assert "detail" not in body
    assert record_directories(fixture) == []


def test_other_users_cannot_read_a_record(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """跨用户统一 404: 详情与材料都读不到, 列表里也看不到别人的记录."""

    owner_fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    with owner_fixture.client as client:
        created = owner_fixture.create()
        review_id = created.json()["review_id"]

    other_fixture = api_client(tmp_path, owner=OTHER_OWNER, monkeypatch=monkeypatch)
    with other_fixture.client as client:
        detail = client.get(f"/api/reviews/{review_id}")
        material = client.get(f"/api/reviews/{review_id}/material")
        listed = client.get("/api/reviews")

    for response in (detail, material):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "review_not_found"
    assert listed.json() == []


def test_list_returns_summaries_without_full_content(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """列表只给摘要: 字段表固定, 不带轮次、工具事件与报告."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)

    with fixture.client as client:
        first = fixture.create()
        second = fixture.create(request_text="请检查这家供应商的材料并说明依据.")
        listed = client.get("/api/reviews", params={"limit": 10})

    assert listed.status_code == 200
    items = listed.json()
    assert [item["review_id"] for item in items] == [
        second.json()["review_id"],
        first.json()["review_id"],
    ]
    assert set(items[0]) == {
        "review_id",
        "original_filename",
        "status",
        "created_at",
        "request_excerpt",
        "finding_count",
        "retry_of_review_id",
    }
    assert items[0]["finding_count"] == 1
    assert "tool_events" not in listed.text
    assert "report" not in listed.text
    assert NODE_KEY not in listed.text


def test_limit_is_validated_for_the_list(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """列表的 limit 只接受 1 到 50; 越界或非数字都得到统一错误体."""

    fixture = api_client(tmp_path, monkeypatch=monkeypatch)

    with fixture.client as client:
        too_small = client.get("/api/reviews", params={"limit": 0})
        too_big = client.get("/api/reviews", params={"limit": 51})
        not_a_number = client.get("/api/reviews", params={"limit": "abc"})

    for response in (too_small, too_big):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    assert not_a_number.status_code == 400
    assert not_a_number.json()["error"]["code"] == "invalid_request"


def test_material_endpoint_returns_the_uploaded_bytes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """材料接口按原字节返回 PDF: 页面用它取 Blob 并定位页码."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    payload = (MATERIALS_DIR / "license_complete.pdf").read_bytes()

    with fixture.client as client:
        created = fixture.create()
        response = client.get(f"/api/reviews/{created.json()['review_id']}/material")
        unknown = client.get("/api/reviews/not-a-uuid/material")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == payload
    assert unknown.status_code == 404


def test_question_round_exposes_the_supplement_action(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """追问轮次: 详情里 allowed_actions 只有 supplement, 页面据此显示补充输入."""

    outcome = make_outcome("请提供营业执照有效期截止日?", kind="question")
    fixture = api_client(tmp_path, outcome=outcome, monkeypatch=monkeypatch)

    with fixture.client:
        created = fixture.create()

    body = created.json()
    assert body["status"] == "question"
    assert body["allowed_actions"] == ["supplement"]
    assert body["rounds"][0]["kind"] == "question"
    assert body["rounds"][0]["report"] is None


def test_agent_failure_is_recorded_with_a_retryable_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Agent 失败: 失败信息与失败轮次一起落盘, 记录标成可重跑."""

    outcome = make_outcome("模型请求失败: Connection error.", kind="failed")
    fixture = api_client(tmp_path, outcome=outcome, monkeypatch=monkeypatch)

    with fixture.client:
        created = fixture.create()

    body = created.json()
    assert body["status"] == "failed"
    assert body["failure"]["code"] == "agent_run_failed"
    assert body["failure"]["message"] == "模型请求失败: Connection error."
    assert body["failure"]["retryable"] is True
    assert body["allowed_actions"] == ["rerun"]
    assert body["rounds"][0]["kind"] == "failed"
    assert body["rounds"][0]["tool_events"][0]["name"] == "read_material"


def test_allowed_actions_are_not_written_to_the_record_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """allowed_actions 是按状态算出的视图字段, 不写进 record.json."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)

    with fixture.client:
        created = fixture.create()

    record_file = fixture.store.root / created.json()["review_id"] / "record.json"
    raw = record_file.read_text(encoding="utf-8")
    assert "allowed_actions" not in raw
    assert created.json()["allowed_actions"] == ["feedback"]


def test_detail_keeps_timestamps_and_material_identity(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """详情里的时间带时区, 材料身份与上传一致."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    payload = (MATERIALS_DIR / "license_complete.pdf").read_bytes()

    with fixture.client:
        body = fixture.create().json()

    assert len(body["material_sha256"]) == 64
    assert body["page_count"] == 1
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(body["rounds"][0]["started_at"]).tzinfo is not None
    assert body["reference_date"] == date(2026, 9, 1).isoformat()
    assert body["rounds"][0]["finished_at"] >= body["rounds"][0]["started_at"]
    assert body["updated_at"] >= body["created_at"]
    assert len(payload) > 0


# ---------------------------------------------------------------------------
# M4-4: 补充, 反馈与重跑
# ---------------------------------------------------------------------------

SETUP_TIME = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
SUPPLEMENT_TEXT = "补充说明: 营业执照有效期至 2030年01月31日"
QUESTION_TEXT = "请提供营业执照有效期截止日?"
FAILURE_TEXT = "模型请求失败: Connection error."


def store_round(outcome: AgentRunOutcome, number: int) -> ReviewRoundRecord:
    """把替身产出转成记录里的一轮, 时间戳固定便于断言."""

    return ReviewRoundRecord.from_outcome(
        outcome,
        number=number,
        model="setup-model",
        started_at=SETUP_TIME,
        finished_at=SETUP_TIME,
    )


def prepare_record(
    fixture: ReviewApiFixture,
    outcome: AgentRunOutcome,
    *,
    owner: UUID = OWNER,
    retryable_failure: bool = True,
) -> str:
    """直接在仓库里把一条记录推到指定状态, 返回 review_id.

    接口用例需要一个已知状态的起点 (追问中 / 已完成 / 失败), 不必每次都从 HTTP 走一遍:
    建档 -> 按固定布局读回材料并登记身份 -> 落一轮结果 (失败轮次同时写 failure).
    """

    store = fixture.store
    record = store.create(
        owner_user_id=owner,
        request_text=REQUEST_TEXT,
        reference_date=date(2026, 9, 1),
        original_filename="license_complete.pdf",
        material_bytes=(MATERIALS_DIR / "license_complete.pdf").read_bytes(),
    )
    document = read_text_pdf(store.material_path_for_owner(record.review_id, owner_user_id=owner))
    record = store.mutate_for_owner(
        record.review_id,
        owner_user_id=owner,
        mutate=lambda current: current.with_material(document),
    )
    if outcome.kind == "failed":
        failure = ReviewFailure(
            code="agent_run_failed", message=outcome.text, retryable=retryable_failure
        )
        return str(
            store.mutate_for_owner(
                record.review_id,
                owner_user_id=owner,
                mutate=lambda current: current.with_round(store_round(outcome, 1), failure=failure),
            ).review_id
        )
    return str(
        store.mutate_for_owner(
            record.review_id,
            owner_user_id=owner,
            mutate=lambda current: current.with_round(store_round(outcome, 1)),
        ).review_id
    )


def test_supplement_runs_the_second_round_and_keeps_the_first(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """补充: 追问轮保留, 第二轮带上补充原文重跑, 结论来自第二轮."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    review_id = prepare_record(fixture, make_outcome(QUESTION_TEXT, kind="question"))

    with fixture.client as client:
        response = client.post(
            f"/api/reviews/{review_id}/supplements", json={"text": SUPPLEMENT_TEXT}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["supplements"] == [SUPPLEMENT_TEXT]
    assert [item["kind"] for item in body["rounds"]] == ["question", "answer"]
    assert body["rounds"][0]["text"] == QUESTION_TEXT
    assert body["rounds"][0]["report"] is None
    assert body["rounds"][1]["report"]["findings"][0]["policy_citations"] == [NODE_KEY]
    assert body["rounds"][1]["model"] == "fake-model"
    assert body["allowed_actions"] == ["feedback"]

    # 第二轮是带着补充原文重新跑的一轮, 材料还是保存的那一份
    command = fixture.model.calls[-1]
    assert command.supplements == (SUPPLEMENT_TEXT,)
    assert command.material.sha256 == body["material_sha256"]
    assert command.user_request == REQUEST_TEXT


def test_supplement_is_allowed_once_and_only_while_waiting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """补充只在 question 状态可用且只能一次: 第一次补充后记录已离开 question."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    review_id = prepare_record(fixture, make_outcome(QUESTION_TEXT, kind="question"))

    with fixture.client as client:
        first = client.post(f"/api/reviews/{review_id}/supplements", json={"text": SUPPLEMENT_TEXT})
        second = client.post(f"/api/reviews/{review_id}/supplements", json={"text": "再补一条"})
        after_rounds = client.get(f"/api/reviews/{review_id}").json()["rounds"]

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "state_conflict"
    assert len(after_rounds) == 2  # 被拒的补充没有长出新的一轮


@pytest.mark.parametrize("text", ["", "   ", "补" * 2001])
def test_supplement_text_is_validated(tmp_path: Path, monkeypatch: MonkeyPatch, text: str) -> None:
    """补充原文长度 1..2000, 空白不算内容: 参数错误一律 400 且不跑第二轮."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    review_id = prepare_record(fixture, make_outcome(QUESTION_TEXT, kind="question"))

    with fixture.client as client:
        response = client.post(f"/api/reviews/{review_id}/supplements", json={"text": text})
        rounds = client.get(f"/api/reviews/{review_id}").json()["rounds"]

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert len(rounds) == 1
    assert fixture.model.calls == []


def test_feedback_is_recorded_once_and_does_not_touch_business_state(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """反馈: 完成报告才能提交, 只记一次, 且明确不改动业务状态."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    review_id = prepare_record(fixture, answer_outcome())

    with fixture.client as client:
        response = client.post(
            f"/api/reviews/{review_id}/feedback",
            json={"decision": "confirmed", "comment": "来源与报告内容已核对"},
        )
        again = client.post(
            f"/api/reviews/{review_id}/feedback", json={"decision": "recheck_requested"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["feedback"]["decision"] == "confirmed"
    assert body["feedback"]["comment"] == "来源与报告内容已核对"
    assert body["business_state_changed"] is False
    assert body["status"] == "completed"
    assert body["allowed_actions"] == []
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "state_conflict"


def test_feedback_requires_a_completed_report_and_valid_input(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """非完成状态不能反馈; 非法 decision 与超长备注都是参数错误."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    waiting = prepare_record(fixture, make_outcome(QUESTION_TEXT, kind="question"))
    completed = prepare_record(fixture, answer_outcome())

    with fixture.client as client:
        not_completed = client.post(
            f"/api/reviews/{waiting}/feedback", json={"decision": "confirmed"}
        )
        bad_decision = client.post(
            f"/api/reviews/{completed}/feedback", json={"decision": "approved"}
        )
        long_comment = client.post(
            f"/api/reviews/{completed}/feedback",
            json={"decision": "confirmed", "comment": "备注" * 600},
        )

    assert not_completed.status_code == 409
    assert not_completed.json()["error"]["code"] == "state_conflict"
    assert bad_decision.status_code == 400
    assert bad_decision.json()["error"]["code"] == "invalid_request"
    assert long_comment.status_code == 400
    assert long_comment.json()["error"]["code"] == "invalid_request"


def test_rerun_creates_a_new_record_and_keeps_the_old_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """重跑: 新 review_id + retry_of_review_id, 旧记录的失败与轮次一点不变."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    failed_id = prepare_record(fixture, make_outcome(FAILURE_TEXT, kind="failed"))

    with fixture.client as client:
        before = client.get(f"/api/reviews/{failed_id}").json()
        created = client.post(f"/api/reviews/{failed_id}/reruns")
        body = created.json()
        after = client.get(f"/api/reviews/{failed_id}").json()
        new_material = client.get(f"/api/reviews/{body['review_id']}/material")
        old_material = client.get(f"/api/reviews/{failed_id}/material")

    assert created.status_code == 201
    assert body["review_id"] != failed_id
    assert body["retry_of_review_id"] == failed_id
    assert body["status"] == "completed"
    assert body["request_text"] == REQUEST_TEXT
    assert body["original_filename"] == "license_complete.pdf"
    assert body["material_id"] == "material"
    assert body["page_count"] == 1
    assert body["material_sha256"] == before["material_sha256"]
    assert new_material.content == old_material.content
    # 旧记录一点没变: 失败与轮次都还在
    assert after == before
    assert after["status"] == "failed"
    assert after["failure"]["message"] == FAILURE_TEXT


def test_rerun_requires_a_retryable_failure_or_a_recheck_request(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """只有可重试失败或已要求重查的完成记录能重跑; 扫描件不可重跑."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    scanned_id = prepare_record(
        fixture, make_outcome("扫描件暂不支持", kind="failed"), retryable_failure=False
    )
    completed_id = prepare_record(fixture, answer_outcome())
    recheck_id = prepare_record(fixture, answer_outcome())

    with fixture.client as client:
        client.post(f"/api/reviews/{recheck_id}/feedback", json={"decision": "recheck_requested"})
        scanned = client.post(f"/api/reviews/{scanned_id}/reruns")
        plain_completed = client.post(f"/api/reviews/{completed_id}/reruns")
        recheck = client.post(f"/api/reviews/{recheck_id}/reruns")

    assert scanned.status_code == 409
    assert scanned.json()["error"]["code"] == "state_conflict"
    assert plain_completed.status_code == 409
    assert recheck.status_code == 201
    assert recheck.json()["retry_of_review_id"] == recheck_id


def test_rerun_ignores_client_supplied_overrides(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """重跑不接受自选模型, 路径或业务状态: 请求体里的这些字段一概不生效."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    failed_id = prepare_record(fixture, make_outcome(FAILURE_TEXT, kind="failed"))
    source_sha256 = fixture.store.get_for_owner(failed_id, owner_user_id=OWNER).material_sha256

    with fixture.client as client:
        created = client.post(
            f"/api/reviews/{failed_id}/reruns",
            json={
                "model": "evil-model",
                "status": "completed",
                "material_path": "C:/evil.pdf",
                "request_text": "换一个请求",
                "reference_date": "2030-01-01",
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["request_text"] == REQUEST_TEXT
    assert body["reference_date"] == date(2026, 9, 1).isoformat()
    assert body["material_sha256"] == source_sha256
    assert body["rounds"][0]["model"] == "fake-model"
    assert body["status"] == "completed"


def test_supplement_feedback_and_rerun_need_ownership(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """三个写接口都核对所有者: 别人的记录一律 404, 且不改动任何记录."""

    fixture = api_client(tmp_path, outcome=answer_outcome(), monkeypatch=monkeypatch)
    waiting = prepare_record(fixture, make_outcome(QUESTION_TEXT, kind="question"))
    completed = prepare_record(fixture, answer_outcome())
    failed = prepare_record(fixture, make_outcome(FAILURE_TEXT, kind="failed"))

    other = api_client(tmp_path, owner=OTHER_OWNER, monkeypatch=monkeypatch)
    with other.client as client:
        supplements = client.post(
            f"/api/reviews/{waiting}/supplements", json={"text": SUPPLEMENT_TEXT}
        )
        feedback = client.post(f"/api/reviews/{completed}/feedback", json={"decision": "confirmed"})
        rerun = client.post(f"/api/reviews/{failed}/reruns")

    for response in (supplements, feedback, rerun):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "review_not_found"
    assert fixture.store.get_for_owner(waiting, owner_user_id=OWNER).status.value == "question"
    assert fixture.store.get_for_owner(completed, owner_user_id=OWNER).feedback is None
    assert len(fixture.store.list_for_owner(owner_user_id=OWNER)) == 3
