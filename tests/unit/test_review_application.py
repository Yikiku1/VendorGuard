"""审查应用层: CLI 与 HTTP 共用的那一条执行链.

M4 方案 4.1 与 5.1 要求"装载依赖并执行一轮"只有一处接线: 应用层在启动时把模型
客户端, 规则与检索数据装载一次, 运行时按 command 新建会话并调用现有 `run_review()`.
本文件钉住它的三条边界:

- 依赖由外部注入: 应用层不复制工具定义, 纠正预算或报告校验, 只把 command 交给循环;
- 每轮按 command 新建会话: 一轮只带自己那份材料与补充原文, 不带上一轮的历史;
- 应用层不写运行记录, 也不碰数据库: 落盘与状态转换是调用方 (CLI / HTTP) 的职责.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

from vendorguard import review_application
from vendorguard.agent import AgentConfigError, AgentRunOutcome
from vendorguard.config import Settings
from vendorguard.policy import load_policy
from vendorguard.review_application import (
    ReviewCommand,
    ReviewRuntime,
    build_review_runtime,
    run_review_round,
)

from .test_agent import (
    REFERENCE_DATE,
    FakeClient,
    ask_user_response,
    check_arguments,
    material,
    policy_index,
    read_response,
    report_response,
    search_response,
    tool_call_response,
)

# 规则文件用绝对路径: 下面把工作目录切到临时目录, 相对路径会找不到文件.
RULES_PATH = Path(__file__).resolve().parents[2] / "policies" / "rules" / "v1.0.0.yaml"
SUPPLEMENT = "补充说明: 营业执照有效期至 2030年01月31日"
FULL_ENV = {
    "VENDORGUARD_LLM_MODEL": "fake-model",
    "VENDORGUARD_LLM_API_KEY": "sk-test-only",
    "VENDORGUARD_LLM_BASE_URL": "https://example.test/v1",
}


@pytest.fixture(scope="module")
def policy():
    """现行规则文档, 循环与应用层测试共用同一份配置."""

    return load_policy(RULES_PATH)


@pytest.fixture(autouse=True)
def clean_cwd(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """把工作目录换成空临时目录: 应用层若写任何文件都落在断言范围内."""

    monkeypatch.chdir(tmp_path)
    return tmp_path


def command(*, supplements: tuple[str, ...] = ()) -> ReviewCommand:
    """构造一条审查命令: 一份固定样例材料, 可选带已登记的补充原文."""

    return ReviewCommand(
        user_request="请检查这家供应商的材料是否满足准入要求.",
        reference_date=REFERENCE_DATE,
        material=material(),
        supplements=supplements,
    )


def runtime_for(policy, client, index=None) -> ReviewRuntime:
    """用替身拼出运行依赖: 客户端与检索数据都由调用方注入."""

    return ReviewRuntime(
        client=client,
        policy=policy,
        policy_index=policy_index() if index is None else index,
        model_name="fake-model",
    )


def test_run_review_round_returns_outcome_and_writes_nothing(policy, clean_cwd) -> None:
    """一轮完整审查返回现有 AgentRunOutcome, 且不在磁盘上留下任何文件."""

    index = policy_index()
    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            search_response(call_id="call_search_1"),
            report_response(call_id="call_report_1", policy_citations=(index.records[0].node_key,)),
        ]
    )

    outcome = run_review_round(command(), runtime=runtime_for(policy, client, index))

    assert isinstance(outcome, AgentRunOutcome)
    assert outcome.kind == "answer"
    assert outcome.report is not None
    assert outcome.report.material_id == "license_complete"
    # 工具循环的顺序没有在应用层被改动过
    assert [event["name"] for event in outcome.tool_events] == [
        "read_material",
        "check_materials",
        "search_policy",
        "submit_report",
    ]
    # 运行记录属于调用方: 应用层跑完一轮不在工作目录留下任何文件
    assert list(clean_cwd.iterdir()) == []


def test_run_review_round_returns_question_when_facts_are_missing(policy) -> None:
    """缺材料事实时返回 kind=question 且没有报告: 追问出口与应用层之前一致."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(valid_until=None), call_id="call_check_1"),
            ask_user_response("请提供营业执照有效期截止日?", call_id="call_ask_1"),
        ]
    )

    outcome = run_review_round(command(), runtime=runtime_for(policy, client))

    assert outcome.kind == "question"
    assert outcome.text == "请提供营业执照有效期截止日?"
    assert outcome.report is None


def test_run_review_round_returns_failed_when_the_model_call_fails(policy) -> None:
    """模型调用失败时返回 kind=failed: 不重试, 也不把失败改写成"依据不足"."""

    class BrokenCompletions:
        def create(self, **kwargs):
            raise ConnectionError("connection reset")

    client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))

    outcome = run_review_round(command(), runtime=runtime_for(policy, client))

    assert outcome.kind == "failed"
    assert "connection reset" in outcome.text
    assert outcome.tool_events == []


def test_run_review_round_starts_a_fresh_session_per_command(policy) -> None:
    """每轮按 command 新建会话: 补充原文只来自本次命令, 不带上一轮的历史."""

    index = policy_index()
    first_client = FakeClient(
        [
            read_response(call_id="call_read_2"),
            tool_call_response(
                check_arguments(valid_until="2030-01-31", date_source="user_supplement@round:1"),
                call_id="call_check_2",
            ),
            search_response(call_id="call_search_2"),
            report_response(
                call_id="call_report_2",
                material_sources=("user_supplement@round:1", "license_complete@page:1"),
                policy_citations=(index.records[0].node_key,),
            ),
        ]
    )

    with_supplement = run_review_round(
        command(supplements=(SUPPLEMENT,)),
        runtime=runtime_for(policy, first_client, index),
    )

    assert with_supplement.kind == "answer"
    assert with_supplement.supplement_rounds == 1
    opening = first_client.completions.calls[0]["messages"][1]["content"]
    assert "user_supplement@round:1" in opening
    assert SUPPLEMENT in opening

    # 同一份依赖再跑一轮不带补充的命令: 新一轮开场不出现上一轮的补充原文
    second_client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            report_response(call_id="call_report_1"),
        ]
    )
    without_supplement = run_review_round(
        command(), runtime=runtime_for(policy, second_client, index)
    )

    assert without_supplement.kind == "answer"
    assert without_supplement.supplement_rounds == 0
    second_opening = second_client.completions.calls[0]["messages"][1]["content"]
    assert "user_supplement" not in second_opening


def test_build_review_runtime_reports_all_missing_config_names(tmp_path: Path) -> None:
    """缺模型配置时一次报全所有缺失的名字, 并且在读写规则与检索数据之前就失败."""

    with pytest.raises(AgentConfigError) as excinfo:
        build_review_runtime(
            {},
            app_settings=Settings(_env_file=None),
            rules_path=tmp_path / "missing.yaml",
            chunk_list_path=tmp_path / "missing.jsonl",
        )

    message = str(excinfo.value)
    assert "VENDORGUARD_LLM_MODEL" in message
    assert "VENDORGUARD_LLM_API_KEY" in message
    assert "VENDORGUARD_LLM_BASE_URL" in message
    # 报的是模型配置而不是那两个不存在的文件: 说明检查顺序在装载依赖之前
    assert "规则文件" not in message
    assert "片段清单" not in message


def test_build_review_runtime_rejects_unusable_rules_file(tmp_path: Path) -> None:
    """规则文件读不出来就是启动失败, 不退化成运行期的"制度里没有相关规定"."""

    with pytest.raises(AgentConfigError, match="规则文件不可用"):
        build_review_runtime(
            FULL_ENV,
            app_settings=Settings(_env_file=None),
            rules_path=tmp_path / "missing.yaml",
        )


def test_build_review_runtime_wires_config_rules_and_index(
    policy, monkeypatch: MonkeyPatch
) -> None:
    """启动装载把配置, 规则, 客户端与检索数据拼成 runtime: 逐项核对来源."""

    index = policy_index()
    created: dict = {}
    index_calls: list[dict] = []

    def fake_openai(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(name="stub-client")

    def fake_index(**kwargs):
        index_calls.append(kwargs)
        return index

    monkeypatch.setattr(review_application, "OpenAI", fake_openai)
    monkeypatch.setattr(review_application, "load_policy_index", fake_index)

    app_settings = Settings(
        _env_file=None,
        embedding_model="qwen3.7-text-embedding",
        embedding_cache_path=Path("data/retrieval/cache/embedding_v1.json"),
    )

    runtime = build_review_runtime(FULL_ENV, app_settings=app_settings, rules_path=RULES_PATH)

    # 模型配置来自环境映射, 规则来自磁盘上的现行文件
    assert created["api_key"] == "sk-test-only"
    assert created["base_url"] == "https://example.test/v1"
    assert runtime.model_name == "fake-model"
    assert runtime.policy.version == policy.version
    assert runtime.policy_index is index
    # embedding 模型与缓存位置来自应用配置, 不是写死的; 检索与原客户端复用同一条接线
    assert index_calls[0]["model"] == "qwen3.7-text-embedding"
    assert index_calls[0]["cache_path"] == app_settings.embedding_cache_path
    assert index_calls[0]["client"] is runtime.client


def test_application_layer_has_no_database_dependency() -> None:
    """应用层不接数据库与业务案件: 它只执行一轮审查.

    M4 方案 4.3: 审查记录是本地演示证据, 不引用 admission_cases, 也不调准入决定
    接口. 这里做源码级检查: 应用层一旦 import 数据库或案件模块, 这个测试立刻变红.
    """

    source = Path(inspect.getsourcefile(review_application)).read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "vendorguard.database" not in source
    assert "vendorguard.admission" not in source
