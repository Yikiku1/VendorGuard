"""用响应替身验证 agent 循环的分支, 预算与错误处理.

替身使用 openai 官方消息类型构造, 保证循环代码对消息
序列化的使用与真实 SDK 一致。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.completion_usage import CompletionUsage

from vendorguard.agent import AgentRunOutcome, run_check, write_run_log
from vendorguard.policy import StructuredFacts, load_policy

RULES_PATH = Path("policies/rules/v1.0.0.yaml")

@pytest.fixture(scope="module")
def policy():
    """加载现行规则配置, 循环测试不触库不联网."""

    return load_policy(RULES_PATH)


def submitted_facts() -> StructuredFacts:
    """构造与工具测试一致的正常事实."""

    return StructuredFacts(
        business_license_document_status="valid",
        category_required_documents_complete=True,
        sources={
            "business_license_document_status": "BL-001@page_1",
            "category_required_documents_complete": "CHECKLIST-001@row_2",
        },
    )


def tool_call_response(
    arguments, call_id: str = "call_test_1", name: str = "check_materials"
) -> ChatCompletion:
    """剧本: 模型要求调用指定工具; arguments 传 str 时原样作为参数文本."""

    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    message = ChatCompletionMessage(
        role="assistant",
        content="",
        tool_calls=[
            ChatCompletionMessageToolCall(
                id=call_id,
                type="function",
                function=Function(name=name, arguments=raw),
            )
        ],
    )
    return _completion(message)


def text_response(text: str) -> ChatCompletion:
    """剧本: 模型直接给出文字回答."""

    return _completion(ChatCompletionMessage(role="assistant", content=text))


def _completion(message: ChatCompletionMessage) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-test",
        choices=[Choice(finish_reason="stop", index=0, message=message)],
        created=0,
        model="test-fake",
        object="chat.completion",
    )


class FakeCompletions:
    """按剧本供给响应, 并记录每次收到的请求参数."""

    def __init__(self, scripted: list[ChatCompletion]) -> None:
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def create(self, **kwargs) -> ChatCompletion:
        # 记录快照而非活引用: 循环在本次调用返回后还会继续向 messages 追加消息
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        assert self.scripted, "模型请求次数超出剧本, 循环可能存在预算泄漏"
        return self.scripted.pop(0)


class FakeClient:
    """伪装 openai.OpenAI 中循环实际用到的那一小块接口."""

    def __init__(self, scripted: list[ChatCompletion]) -> None:
        self.completions = FakeCompletions(scripted)
        self.chat = SimpleNamespace(completions=self.completions)


def test_normal_round_trip_returns_answer(policy) -> None:
    """正常往返: 模型调用工具, 程序执行, 模型基于结果回答."""

    client = FakeClient(
        [
            tool_call_response(submitted_facts().model_dump()),
            text_response("本次检查两条规则均为 not_hit, 未命中不代表准入批准."),
        ]
    )

    outcome = run_check(
        "请检查这家供应商的材料", submitted=submitted_facts(), policy=policy, client=client
    )

    assert outcome.kind == "answer"
    assert "not_hit" in outcome.text
    second_messages = client.completions.calls[1]["messages"]
    assert [m["role"] for m in second_messages] == ["system", "user", "assistant", "tool"]
    tool_message = second_messages[3]
    assert tool_message["tool_call_id"] == "call_test_1"
    payload = json.loads(tool_message["content"])
    assert [e["result"] for e in payload["evaluations"]] == ["not_hit", "not_hit"]


def test_bad_json_gets_structured_error_and_correction(policy) -> None:
    """第一次调用参数是坏 JSON: 不执行, 按调用 ID 回传结构化错误, 模型一次纠正成功."""

    client = FakeClient(
        [
            tool_call_response("{ not valid json"),
            tool_call_response(submitted_facts().model_dump(), call_id="call_test_2"),
            text_response("重新检查完成, 两条规则均为 not_hit, 未命中不代表准入批准."),
        ]
    )

    outcome = run_check(
        "请检查这家供应商的材料", submitted=submitted_facts(), policy=policy, client=client
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 3
    assert outcome.tool_attempts == 2
    assert outcome.tool_events[0]["status"] == "error"
    assert outcome.tool_events[0]["name"] == "check_materials"
    assert "不是合法 JSON" in outcome.tool_events[0]["detail"]
    error_message = client.completions.calls[1]["messages"][-1]
    assert error_message["role"] == "tool"
    assert error_message["tool_call_id"] == "call_test_1"
    assert "error" in json.loads(error_message["content"])


def test_second_bad_json_exhausts_correction(policy) -> None:
    """纠正机会全局只有一次: 第二次坏参数直接明确失败, 不再发请求."""

    client = FakeClient(
        [
            tool_call_response("{ bad"),
            tool_call_response("{ still bad", call_id="call_test_2"),
        ]
    )

    outcome = run_check(
        "请检查这家供应商的材料", submitted=submitted_facts(), policy=policy, client=client
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 2
    assert outcome.tool_attempts == 2


# --- 剧本三: 剩余分支验收 -----------------------------------------------------


def test_unknown_tool_rejected_then_corrected(policy) -> None:
    """未知工具不执行, 结构化错误按调用 ID 回传, 模型一次纠正后成功."""

    client = FakeClient(
        [
            tool_call_response({}, call_id="call_evil", name="delete_all_suppliers"),
            tool_call_response(submitted_facts().model_dump(), call_id="call_test_2"),
            text_response("检查完成, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_check("请检查", submitted=submitted_facts(), policy=policy, client=client)

    assert outcome.kind == "answer"
    assert outcome.model_requests == 3
    error_message = client.completions.calls[1]["messages"][-1]
    assert error_message["role"] == "tool"
    assert error_message["tool_call_id"] == "call_evil"
    assert "未知工具" in json.loads(error_message["content"])["error"]


def test_fabricated_arguments_rejected_then_corrected(policy) -> None:
    """参数合法 JSON 但与快照不符(改写)时同样走纠正链."""

    fabricated = {**submitted_facts().model_dump(), "category_required_documents_complete": False}
    client = FakeClient(
        [
            tool_call_response(fabricated),
            tool_call_response(submitted_facts().model_dump(), call_id="call_test_2"),
            text_response("按快照事实复核, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_check("请检查", submitted=submitted_facts(), policy=policy, client=client)

    assert outcome.kind == "answer"
    error_message = client.completions.calls[1]["messages"][-1]
    assert "不一致" in json.loads(error_message["content"])["error"]


def test_false_claim_without_call_forces_correction(policy) -> None:
    """未执行工具就声称已检查: 运行器以 user 消息要求纠正, 最多一次."""

    client = FakeClient(
        [
            text_response("检查完成, 两条规则均为 not_hit, 未命中不代表准入批准."),
            tool_call_response(submitted_facts().model_dump(), call_id="call_test_2"),
            text_response("根据工具结果: 两条规则均为 not_hit."),
        ]
    )

    outcome = run_check("请检查", submitted=submitted_facts(), policy=policy, client=client)

    assert outcome.kind == "answer"
    assert outcome.model_requests == 3
    correction = client.completions.calls[1]["messages"][-1]
    assert correction["role"] == "user"
    assert "check_materials" in correction["content"]


def test_followup_question_ends_run(policy) -> None:
    """信息不足时允许直接追问: 一次请求即结束, 零工具调用."""

    client = FakeClient([text_response("请先补充该品类的必填材料清单, 我再重新检查.")])

    outcome = run_check(
        "帮我看看这家供应商", submitted=submitted_facts(), policy=policy, client=client
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 1
    assert outcome.tool_attempts == 0


def test_model_request_budget_cap(policy) -> None:
    """模型反复调用工具时请求数封顶, 达到上限明确失败."""

    client = FakeClient(
        [
            tool_call_response(submitted_facts().model_dump(), call_id=f"call_loop_{i}")
            for i in range(8)
        ]
    )

    outcome = run_check("请检查", submitted=submitted_facts(), policy=policy, client=client)

    assert outcome.kind == "failed"
    assert outcome.model_requests == 8
    assert "上限" in outcome.text


def test_negative_total_timeout_fails_before_first_request(policy) -> None:
    """总时限用负数预算触发: 第一次循环检查即失败, 不发任何请求."""

    client = FakeClient([])

    outcome = run_check(
        "请检查", submitted=submitted_facts(), policy=policy, client=client, total_timeout=-1.0
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 0
    assert client.completions.calls == []


def test_request_timeout_clamped_by_remaining_budget(policy) -> None:
    """每轮传给 SDK 的 timeout 必须被剩余总时限压低, 而不是用单请求上限."""

    client = FakeClient([text_response("完成.")])

    run_check(
        "请检查",
        submitted=submitted_facts(),
        policy=policy,
        client=client,
        request_timeout=30.0,
        total_timeout=1.0,
    )

    sent_timeout = client.completions.calls[0]["timeout"]
    assert 0 < sent_timeout <= 1.0


def test_network_failure_ends_immediately(policy) -> None:
    """网络异常直接明确失败并保留计数, 不重试不吞错."""

    class BrokenCompletions:
        def create(self, **kwargs):
            raise ConnectionError("connection reset")

    client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))

    outcome = run_check("请检查", submitted=submitted_facts(), policy=policy, client=client)

    assert outcome.kind == "failed"
    assert outcome.model_requests == 1


# --- E1-a: token 用量与工具事件流水 ---------------------------------------------------


def with_usage(completion: ChatCompletion, prompt: int, generated: int) -> ChatCompletion:
    """给剧本响应补上 token 用量, 模拟真实 SDK 的 usage 字段."""

    return completion.model_copy(
        update={
            "usage": CompletionUsage(
                completion_tokens=generated,
                prompt_tokens=prompt,
                total_tokens=prompt + generated,
            )
        }
    )


def test_outcome_reports_usage_totals_and_tool_events(policy) -> None:
    """正常往返后累计跨请求的 token 用量, 并记录工具事件流水."""

    client = FakeClient(
        [
            with_usage(tool_call_response(submitted_facts().model_dump()), 100, 20),
            with_usage(text_response("检查完成, 两条规则均为 not_hit."), 200, 30),
        ]
    )

    outcome = run_check(
        "请检查这家供应商的材料", submitted=submitted_facts(), policy=policy, client=client
    )

    assert outcome.prompt_tokens == 300
    assert outcome.completion_tokens == 50
    assert len(outcome.tool_events) == 1
    event = outcome.tool_events[0]
    assert event["name"] == "check_materials"
    assert event["status"] == "ok"
    assert event["tool_call_id"] == "call_test_1"
    assert [e["result"] for e in event["detail"]["evaluations"]] == ["not_hit", "not_hit"]


def test_unknown_tool_recorded_as_error_event(policy) -> None:
    """未知工具与成功调用都要入流水, 状态分别为 error 和 ok."""

    client = FakeClient(
        [
            tool_call_response({}, call_id="call_evil", name="drop_all_tables"),
            tool_call_response(submitted_facts().model_dump(), call_id="call_ok"),
            text_response("最终只有真实检查结果可信."),
        ]
    )

    outcome = run_check(
        "请检查这家供应商的材料", submitted=submitted_facts(), policy=policy, client=client
    )

    assert [event["status"] for event in outcome.tool_events] == ["error", "ok"]
    assert outcome.tool_events[0]["tool_call_id"] == "call_evil"


# --- E1-b: 运行记录器 ------------------------------------------------------------


def make_outcome(text: str = "两条规则均为 not_hit.", kind: str = "answer") -> AgentRunOutcome:
    """构造一份标准运行产出, 供记录器测试复用."""

    return AgentRunOutcome(
        kind=kind,
        text=text,
        model_requests=2,
        tool_attempts=1,
        elapsed_seconds=1.25,
        prompt_tokens=300,
        completion_tokens=50,
        tool_events=[
            {
                "tool_call_id": "call_1",
                "name": "check_materials",
                "status": "ok",
                "detail": {"policy_version": "1.0.0"},
            }
        ],
    )


def test_write_run_log_records_expected_fields(tmp_path) -> None:
    """运行记录写入带时间戳的 JSON 文件, 字段覆盖方案要求."""

    path = write_run_log(
        make_outcome(),
        model_name="qwen3.7-flash",
        log_dir=tmp_path,
        started_at=datetime(2026, 9, 9, 10, 30, 0),
    )

    assert path.parent == tmp_path
    assert path.name.startswith("20260909T103000")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["model"] == "qwen3.7-flash"
    assert payload["simulated_input"] is True
    assert payload["kind"] == "answer"
    assert payload["text"] == "两条规则均为 not_hit."
    assert payload["usage"] == {"prompt_tokens": 300, "completion_tokens": 50}
    assert payload["tool_events"][0]["status"] == "ok"
    assert payload["elapsed_seconds"] == 1.25


def test_write_run_log_redacts_api_key_style_secrets(tmp_path) -> None:
    """文本里意外混入 sk- 样式密钥时, 落盘前必须整体打码."""

    path = write_run_log(
        make_outcome(text="模型请求失败: Authorization: sk-abcdefghij1234567890", kind="failed"),
        model_name="qwen3.7-flash",
        log_dir=tmp_path,
        started_at=datetime(2026, 9, 9, 10, 30, 0),
    )

    written = path.read_text(encoding="utf-8")
    assert "sk-abcdefghij1234567890" not in written
    assert "sk-***" in written