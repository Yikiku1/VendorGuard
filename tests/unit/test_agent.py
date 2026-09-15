"""用响应替身验证材料审查循环的分支, 顺序规则, 预算与错误处理.

替身使用 openai 官方消息类型构造, 保证循环代码对消息序列化的使用与真实
SDK 一致。M2 的契约变化: 循环接收一份已登记材料, 模型必须先调用
read_material 读材料, 再调用 check_materials 提交"从材料读到的事实 + 来源
页码"; 未读材料就 check、来源核对失败、缺字段后直接给结论都会被拒绝。
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.completion_usage import CompletionUsage

from vendorguard.agent import (
    AgentConfigError,
    AgentRunOutcome,
    PolicyIndex,
    load_llm_settings,
    load_material,
    load_policy_index,
    run_review,
    write_run_log,
)
from vendorguard.agent_session import ReviewSession
from vendorguard.materials import MaterialDocument
from vendorguard.policy import load_policy
from vendorguard.retrieval.chunk_list import CURRENT_EDITION_KEYS, ChunkRecord
from vendorguard.retrieval.embedding import EMBEDDING_DIMENSION, ChunkVectors

RULES_PATH = Path("policies/rules/v1.0.0.yaml")
CHUNK_LIST_PATH = Path("data/retrieval/chunks_v1.jsonl")
MATERIALS_DIR = Path(__file__).resolve().parents[2] / "data" / "demo" / "materials"
# 与 data/demo/cases/*.yaml 的 reference_date 一致; 用来把声明有效期派生成状态。
REFERENCE_DATE = date(2026, 9, 1)

# 材料正文按固定样例的内容构造, 所以循环测试不需要读真实 PDF。
MATERIAL_PAGE_TEXT = "营业执照(演示样例, 非真实证照)\n声明有效期至: 2027-08-31\n材料清单状态: 齐全"


@pytest.fixture(scope="module")
def policy():
    """加载现行规则配置, 循环测试不触库不联网."""

    return load_policy(RULES_PATH)


def material(material_id: str = "license_complete") -> MaterialDocument:
    """构造一份单页文本材料, 与固定样例 license_complete.pdf 内容同形."""

    return MaterialDocument(
        material_id=material_id,
        source_name=f"{material_id}.pdf",
        sha256="0" * 64,
        page_count=1,
        pages=(MATERIAL_PAGE_TEXT,),
    )


def read_response(
    material_id: str = "license_complete", call_id: str = "call_read_1"
) -> ChatCompletion:
    """剧本: 模型调用 read_material 读取本次材料."""

    return tool_call_response({"material_id": material_id}, call_id=call_id, name="read_material")


def check_arguments(
    *,
    valid_until: str | None = "2027-08-31",
    docs_complete: bool | None = True,
    date_source: str = "license_complete@page:1",
    checklist_source: str = "license_complete@page:1",
) -> dict:
    """构造 check_materials 参数: 从材料读到的事实 + 来源定位.

    传 None 表示该事实没读到, 连同来源一起省略; 两个来源分开给, 因为
    日期可能来自用户补充而清单来自材料页。
    """

    payload: dict = {}
    sources: dict = {}
    if valid_until is not None:
        payload["business_license_valid_until"] = valid_until
        sources["business_license_valid_until"] = date_source
    if docs_complete is not None:
        payload["category_required_documents_complete"] = docs_complete
        sources["category_required_documents_complete"] = checklist_source
    payload["sources"] = sources
    return payload


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


def text_response(text: str, finish_reason: str = "stop") -> ChatCompletion:
    """剧本: 模型直接给出文字回答, 可指定结束原因."""

    return _completion(ChatCompletionMessage(role="assistant", content=text), finish_reason)


def ask_user_response(question: str, call_id: str = "call_ask_1") -> ChatCompletion:
    """剧本: 模型调用 ask_user 追问工具."""

    return tool_call_response({"question": question}, call_id=call_id, name="ask_user")


def _completion(message: ChatCompletionMessage, finish_reason: str = "stop") -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-test",
        choices=[Choice(finish_reason=finish_reason, index=0, message=message)],
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


class FakeEmbeddings:
    """检索用的 embedding 替身: 默认返回指向第 0 维的单位向量, 可改成报错.

    与 M3-2 的替身同一契约: 返回项带 index, 向量是 1024 维单位长度.
    """

    def __init__(
        self,
        *,
        dimension: int = EMBEDDING_DIMENSION,
        error: Exception | None = None,
    ) -> None:
        self.dimension = dimension
        self.error = error
        self.requests: list[list[str]] = []

    def create(self, **kwargs) -> SimpleNamespace:
        payload = kwargs["input"]
        self.requests.append([str(item) for item in payload])
        if self.error is not None:
            raise self.error
        vector = [1.0, *([0.0] * (self.dimension - 1))]
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=list(vector))
                for index in range(len(self.requests[-1]))
            ],
            usage=SimpleNamespace(prompt_tokens=1),
        )


class FakeClient:
    """伪装 openai.OpenAI 中循环实际用到的那一小块接口."""

    def __init__(
        self,
        scripted: list[ChatCompletion],
        embeddings: FakeEmbeddings | None = None,
    ) -> None:
        self.completions = FakeCompletions(scripted)
        self.chat = SimpleNamespace(completions=self.completions)
        self.embeddings = embeddings if embeddings is not None else FakeEmbeddings()


def search_response(
    query: str = "关键物料可以先准入后补交质量证书吗",
    *,
    top_k: int | None = None,
    call_id: str = "call_search_1",
) -> ChatCompletion:
    """剧本: 模型调用 search_policy 检索制度条款."""

    arguments: dict = {"query": query}
    if top_k is not None:
        arguments["top_k"] = top_k
    return tool_call_response(arguments, call_id=call_id, name="search_policy")


def chunk_record(index: int, *, edition_key: str = "demo_supplier_admission_policy_v2"):
    """造一条现行制度的片段, 只用于给检索提供可引用的节点."""

    text = f"制度正文第{index}条"
    return ChunkRecord(
        chunk_key=f"{edition_key}_section_{index}_main",
        node_key=f"{edition_key}_section_{index}",
        edition_key=edition_key,
        document_title="探针制度",
        locator_path=(f"第{index}节",),
        display_text=text,
        search_text=f"探针制度 第{index}节\n{text}",
        char_start=0,
        char_end=len(text),
        source_path="normalized/probe.txt",
        snapshot_sha256="a" * 64,
        normalized_sha256="b" * 64,
    )


def policy_index(records: int = 2, *, model: str = "qwen3.7-text-embedding") -> PolicyIndex:
    """造一份两段的检索数据: 第 0 条与查询同向 (相似度 1.0), 之后依次远离."""

    items = tuple(chunk_record(index) for index in range(1, records + 1))
    vectors = []
    for position in range(records):
        weights = [0.0] * EMBEDDING_DIMENSION
        weights[0] = 1.0
        if position:
            weights[position] = 1.0
        norm = math.sqrt(sum(value * value for value in weights))
        vectors.append(tuple(value / norm for value in weights))
    return PolicyIndex(
        records=items,
        vectors=ChunkVectors(
            model=model,
            cache_key="c" * 64,
            dimension=EMBEDDING_DIMENSION,
            vectors=tuple(vectors),
        ),
        model=model,
    )


def tool_payload(client: FakeClient, request_index: int, tool_call_id: str) -> dict:
    """取出某次模型请求里某个工具调用的结果 JSON."""

    for message in client.completions.calls[request_index]["messages"]:
        if message.get("role") == "tool" and message.get("tool_call_id") == tool_call_id:
            return json.loads(message["content"])
    raise AssertionError(f"第 {request_index} 次请求里没有找到 {tool_call_id} 的工具结果")


def test_normal_round_trip_returns_answer(policy) -> None:
    """正常往返: 读材料 -> 校验 -> 模型基于工具结果作答."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("本次检查两条规则均为 not_hit, 未命中不代表准入批准."),
        ]
    )

    outcome = run_review(
        "请检查这家供应商的材料",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert "not_hit" in outcome.text
    read_messages = client.completions.calls[1]["messages"]
    assert [m["role"] for m in read_messages] == ["system", "user", "assistant", "tool"]
    read_result = json.loads(read_messages[3]["content"])
    assert read_result["material_id"] == "license_complete"
    assert read_result["pages"][0]["page"] == 1
    assert "声明有效期至: 2027-08-31" in read_result["pages"][0]["text"]

    check_messages = client.completions.calls[2]["messages"]
    check_result = json.loads(check_messages[5]["content"])
    assert check_messages[5]["tool_call_id"] == "call_test_1"
    assert [e["result"] for e in check_result["evaluations"]] == ["not_hit", "not_hit"]
    assert check_result["derived_business_license_status"] == "valid"


def test_read_material_is_recorded_as_ok_event(policy) -> None:
    """读取材料成功要入流水: 记材料 ID 与页数, 供运行记录复盘."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    read_event = outcome.tool_events[0]
    assert read_event["name"] == "read_material"
    assert read_event["status"] == "ok"
    assert read_event["detail"] == {
        "material_id": "license_complete",
        "page_count": 1,
    }
    assert outcome.material_id == "license_complete"
    assert outcome.material_sha256 == "0" * 64


def test_material_must_be_read_before_check(policy) -> None:
    """顺序规则: 没读材料就校验会被拒绝, 并提示先调用 read_material."""

    client = FakeClient(
        [
            tool_call_response(check_arguments()),
            read_response(),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("复核完成, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 4
    rejected = outcome.tool_events[0]
    assert rejected["status"] == "error"
    assert "read_material" in rejected["detail"]
    correction = client.completions.calls[1]["messages"][-1]
    assert correction["role"] == "tool"
    assert "read_material" in json.loads(correction["content"])["error"]


def test_check_without_read_exhausts_correction(policy) -> None:
    """顺序规则不是提示词: 反复跳过读取直接校验, 纠正用尽即明确失败."""

    client = FakeClient(
        [
            tool_call_response(check_arguments()),
            tool_call_response(check_arguments(), call_id="call_test_2"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 2
    assert [event["status"] for event in outcome.tool_events] == ["error", "error"]
    assert "check_materials" not in [
        event["name"] for event in outcome.tool_events if event["status"] == "ok"
    ]


def test_read_material_rejects_unregistered_material_id(policy) -> None:
    """读取材料只能读本次会话登记的那一份, 别的 ID 一律拒绝."""

    client = FakeClient(
        [
            read_response("other_supplier_license"),
            read_response(),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("复核完成, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    rejected = outcome.tool_events[0]
    assert rejected["status"] == "error"
    assert "other_supplier_license" in rejected["detail"]


def test_fabricated_date_source_mismatch_is_rejected(policy) -> None:
    """来源核对: 模型编一个材料里没有的日期, 规则不执行并给一次纠正."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(valid_until="2030-01-01")),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("复核完成, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    rejected = outcome.tool_events[1]
    assert rejected["status"] == "error"
    assert rejected["name"] == "check_materials"
    assert "business_license_valid_until" in rejected["detail"]


def test_opposite_checklist_source_mismatch_is_rejected(policy) -> None:
    """来源核对: 材料写着"齐全"而模型报不齐全, 同样是事实与原文不一致."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(docs_complete=False)),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("复核完成, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.tool_events[1]["status"] == "error"
    assert "category_required_documents_complete" in outcome.tool_events[1]["detail"]


def test_derived_status_follows_reference_date(policy) -> None:
    """状态由程序派生: 同一份材料换个参考日期, 结论从 not_hit 变成 hit."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("根据工具结果: VEN-001 命中, 处置为补件建议."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=date(2027, 9, 1),
        policy=policy,
        client=client,
    )

    check_event = outcome.tool_events[1]
    assert check_event["detail"]["derived_business_license_status"] == "expired"
    evaluations = {e["rule_id"]: e for e in check_event["detail"]["evaluations"]}
    assert evaluations["VEN-001"]["result"] == "hit"
    assert evaluations["VEN-001"]["outcome"]["reason_code"] == "business_license_expired"


def test_tool_result_is_followed_by_read_only_reminder(policy) -> None:
    """工具给出建议后, 下一次模型请求必须明确本次运行没有写入案件状态."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("两条规则均为 not_hit。"),
        ]
    )

    run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    reminder = client.completions.calls[2]["messages"][-1]
    assert reminder["role"] == "user"
    assert "没有写入案件状态" in reminder["content"]


def test_bad_json_gets_structured_error_and_correction(policy) -> None:
    """第一次调用参数是坏 JSON: 不执行, 按调用 ID 回传结构化错误, 模型一次纠正成功."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response("{ not valid json"),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("重新检查完成, 两条规则均为 not_hit, 未命中不代表准入批准."),
        ]
    )

    outcome = run_review(
        "请检查这家供应商的材料",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 4
    assert outcome.tool_attempts == 3
    assert outcome.tool_events[1]["status"] == "error"
    assert outcome.tool_events[1]["name"] == "check_materials"
    assert "不是合法 JSON" in outcome.tool_events[1]["detail"]
    error_message = client.completions.calls[2]["messages"][-1]
    assert error_message["role"] == "tool"
    assert error_message["tool_call_id"] == "call_test_1"
    assert "error" in json.loads(error_message["content"])


def test_second_bad_json_exhausts_correction(policy) -> None:
    """纠正机会全局只有一次: 第二次坏参数直接明确失败, 不再发请求."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response("{ bad"),
            tool_call_response("{ still bad", call_id="call_test_2"),
        ]
    )

    outcome = run_review(
        "请检查这家供应商的材料",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 3
    assert outcome.tool_attempts == 3
    # 审查修复 #4: 被拒绝的调用也必须入流水(读取材料那次是 ok)
    assert [event["status"] for event in outcome.tool_events] == ["ok", "error", "error"]


# --- 剧本三: 剩余分支验收 -----------------------------------------------------


def test_unknown_tool_rejected_then_corrected(policy) -> None:
    """未知工具不执行, 结构化错误按调用 ID 回传, 模型一次纠正后成功."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response({}, call_id="call_evil", name="delete_all_suppliers"),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("检查完成, 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 4
    error_message = client.completions.calls[2]["messages"][-1]
    assert error_message["role"] == "tool"
    assert error_message["tool_call_id"] == "call_evil"
    assert "未知工具" in json.loads(error_message["content"])["error"]


def test_false_claim_without_call_forces_correction(policy) -> None:
    """未执行工具就声称已检查: 运行器以 user 消息要求纠正, 最多一次."""

    client = FakeClient(
        [
            text_response("检查完成, 两条规则均为 not_hit, 未命中不代表准入批准."),
            read_response(),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("根据工具结果: 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 4
    correction = client.completions.calls[1]["messages"][-1]
    assert correction["role"] == "user"
    assert "check_materials" in correction["content"]


def test_text_answer_before_check_does_not_end_run(policy) -> None:
    """只读了材料还没校验就下结论: 同样不算完成, 必须纠正后再校验."""

    client = FakeClient(
        [
            read_response(),
            text_response("材料看起来没问题, 符合准入要求."),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("根据工具结果: 两条规则均为 not_hit."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 4
    assert [event["status"] for event in outcome.tool_events] == ["ok", "error", "ok"]
    assert outcome.tool_events[1]["name"] == "<text-correction>"


def test_followup_question_ends_run(policy) -> None:
    """纯文本问句不再直接放行: 经一次纠正后改用 ask_user 才能以 question 结束."""

    client = FakeClient(
        [
            text_response("请问该品类的必填材料清单是什么? 请补充后我再重新检查."),
            ask_user_response("该品类的必填材料清单是什么? 请提供后我再检查."),
        ]
    )

    outcome = run_review(
        "帮我看看这家供应商",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert outcome.model_requests == 2
    assert outcome.tool_attempts == 1


def test_model_request_budget_cap(policy) -> None:
    """模型反复读材料时请求数封顶, 达到上限明确失败."""

    client = FakeClient([read_response(call_id=f"call_loop_{i}") for i in range(8)])

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 8
    assert "上限" in outcome.text


def test_negative_total_timeout_fails_before_first_request(policy) -> None:
    """总时限用负数预算触发: 第一次循环检查即失败, 不发任何请求."""

    client = FakeClient([])

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        total_timeout=-1.0,
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 0
    assert client.completions.calls == []


def test_request_timeout_clamped_by_remaining_budget(policy) -> None:
    """每轮传给 SDK 的 timeout 必须被剩余总时限压低, 而不是用单请求上限."""

    client = FakeClient([text_response("完成.")])

    run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
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

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

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
            with_usage(read_response(), 100, 20),
            with_usage(tool_call_response(check_arguments()), 100, 20),
            with_usage(text_response("检查完成, 两条规则均为 not_hit."), 100, 10),
        ]
    )

    outcome = run_review(
        "请检查这家供应商的材料",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.prompt_tokens == 300
    assert outcome.completion_tokens == 50
    assert len(outcome.tool_events) == 2
    check_event = outcome.tool_events[1]
    assert check_event["name"] == "check_materials"
    assert check_event["status"] == "ok"
    assert check_event["tool_call_id"] == "call_test_1"
    assert [e["result"] for e in check_event["detail"]["evaluations"]] == ["not_hit", "not_hit"]


def test_unknown_tool_recorded_as_error_event(policy) -> None:
    """未知工具与成功调用都要入流水, 状态分别为 error 和 ok."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response({}, call_id="call_evil", name="drop_all_tables"),
            tool_call_response(check_arguments(), call_id="call_ok"),
            text_response("最终只有真实检查结果可信."),
        ]
    )

    outcome = run_review(
        "请检查这家供应商的材料",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert [event["status"] for event in outcome.tool_events] == ["ok", "error", "ok"]
    assert outcome.tool_events[1]["tool_call_id"] == "call_evil"


# --- E1-b: 运行记录器 ------------------------------------------------------------


def make_outcome(text: str = "两条规则均为 not_hit.", kind: str = "answer") -> AgentRunOutcome:
    """构造一份标准运行产出, 供记录器测试复用."""

    return AgentRunOutcome(
        kind=kind,
        text=text,
        model_requests=3,
        tool_attempts=2,
        elapsed_seconds=1.25,
        prompt_tokens=300,
        completion_tokens=50,
        tool_events=[
            {
                "tool_call_id": "call_read_1",
                "name": "read_material",
                "status": "ok",
                "detail": {"material_id": "license_complete", "page_count": 1},
            },
            {
                "tool_call_id": "call_1",
                "name": "check_materials",
                "status": "ok",
                "detail": {"policy_version": "1.0.0"},
            },
        ],
        user_request="请检查这家供应商的材料",
        policy_version="1.0.0",
        material_id="license_complete",
        material_sha256="a" * 64,
        reference_date="2026-09-01",
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
    assert payload["tool_events"][1]["status"] == "ok"
    assert payload["elapsed_seconds"] == 1.25
    # 材料身份必须入档: 哈希决定"这次审查的是哪一份文件"
    assert payload["material_id"] == "license_complete"
    assert payload["material_sha256"] == "a" * 64
    assert payload["reference_date"] == "2026-09-01"


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


# --- E1-c: 启动入口配置与材料加载 -------------------------------------------------


# 运行时生成测试密钥, 使源码中不出现凭据形状的字面量; 前缀仅用于断言报错不外泄密钥.
TEST_API_KEY = f"sk-{uuid4().hex}"
FULL_ENV = {
    "VENDORGUARD_LLM_MODEL": "qwen3.7-flash",
    "VENDORGUARD_LLM_API_KEY": TEST_API_KEY,
    "VENDORGUARD_LLM_BASE_URL": "https://example.test/compatible-mode/v1",
}


def test_load_llm_settings_reports_all_missing_names() -> None:
    """空环境一次报出全部缺失变量, 不挤牙膏逐个试错."""

    with pytest.raises(AgentConfigError) as exc_info:
        load_llm_settings({})

    message = str(exc_info.value)
    assert "VENDORGUARD_LLM_MODEL" in message
    assert "VENDORGUARD_LLM_API_KEY" in message
    assert "VENDORGUARD_LLM_BASE_URL" in message
    assert "sk-" not in message  # 报错消息自身不得携带任何密钥样式内容


def test_load_llm_settings_treats_empty_value_as_missing() -> None:
    """变量存在但值为空同样算缺失, 覆盖 .env 里写了 KEY= 的情况."""

    env = {**FULL_ENV, "VENDORGUARD_LLM_API_KEY": ""}
    with pytest.raises(AgentConfigError, match="VENDORGUARD_LLM_API_KEY"):
        load_llm_settings(env)


def test_load_llm_settings_returns_values_when_complete() -> None:
    """三项齐备时逐字段映射到配置对象."""

    settings = load_llm_settings(FULL_ENV)

    assert settings.model_name == "qwen3.7-flash"
    assert settings.api_key == TEST_API_KEY
    assert settings.base_url == "https://example.test/compatible-mode/v1"


def test_load_material_reads_fixed_sample() -> None:
    """启动入口读取固定样例: 材料 ID 取文件名主干, 页面文本可核对."""

    loaded = load_material(MATERIALS_DIR / "license_complete.pdf")

    assert loaded.material_id == "license_complete"
    assert loaded.page_count == 1
    assert "声明有效期至: 2027-08-31" in loaded.pages[0]


def test_load_material_rejects_bad_input(tmp_path) -> None:
    """文件不存在或不是文本 PDF 都归一为 AgentConfigError, 不向外抛裸异常."""

    with pytest.raises(AgentConfigError):
        load_material(tmp_path / "nope.pdf")

    scanned = tmp_path / "scanned.pdf"
    scanned.write_bytes((MATERIALS_DIR / "license_scanned.pdf").read_bytes())
    with pytest.raises(AgentConfigError, match="扫描件"):
        load_material(scanned)


# --- 评审收尾: 结论背书, 时效与记录完整性 --------------------------------------


def test_unverified_claim_gets_correction_then_answer(policy) -> None:
    """审查 P1 复现: 无工具就给结论不放行, 先纠正, 纠正后才可下结论."""

    client = FakeClient(
        [
            text_response("材料齐全, 营业执照有效, 符合准入要求。"),
            read_response(),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("根据工具结果: 两条规则均为 not_hit。"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "answer"
    assert outcome.model_requests == 4
    correction = client.completions.calls[1]["messages"][-1]
    assert correction["role"] == "user"
    assert "check_materials" in correction["content"]


def test_second_unverified_claim_fails(policy) -> None:
    """纠正后仍拒绝调用工具: 直接失败, 结论必须工具背书."""

    client = FakeClient(
        [
            text_response("材料齐全, 符合准入要求。"),
            text_response("我确认无需工具, 符合要求。"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 2


def test_empty_final_text_fails(policy) -> None:
    """空回答不是成功产出, 不得以 answer 或 question 收场."""

    client = FakeClient([text_response("")])

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"


def test_truncated_response_fails(policy) -> None:
    """finish_reason 非 stop 的回答可能被截断, 不得当作完整结论."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("检查完成, 两条规则均为 not", finish_reason="length"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"


def test_stale_response_fails_after_total_timeout(policy) -> None:
    """响应返回时总预算已尽: 迟到的回答不得标记成功."""

    class SlowCompletions:
        def create(self, **kwargs):
            time.sleep(0.05)
            return text_response("迟到的结论不可信。")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions()))

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        total_timeout=0.01,
    )

    assert outcome.kind == "failed"
    assert "时限" in outcome.text


def test_events_carry_raw_arguments(policy) -> None:
    """每个工具事件必须带模型原始参数字符串, 含被拒绝的调用."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response("{ bad args"),
            tool_call_response(check_arguments(), call_id="call_test_2"),
            text_response("复核完成。"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.tool_events[1]["arguments"] == "{ bad args"
    assert "category_required_documents_complete" in outcome.tool_events[2]["arguments"]


def test_outcome_carries_context_for_log(policy) -> None:
    """运行产出携带用户请求, 材料身份与参考日期供记录器使用."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("完成。"),
        ]
    )

    outcome = run_review(
        "请检查这家供应商的材料",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.user_request == "请检查这家供应商的材料"
    assert outcome.material_id == "license_complete"
    assert outcome.material_sha256 == "0" * 64
    assert outcome.reference_date == "2026-09-01"
    assert outcome.policy_version == "1.0.0"


def test_run_log_includes_request_context(policy, tmp_path) -> None:
    """落盘记录必须足以复现: 请求, 材料身份, 参考日期与规则版本都要在档."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            text_response("完成。"),
        ]
    )
    outcome = run_review(
        "请检查这家供应商",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    path = write_run_log(
        outcome,
        model_name="qwen3.7-flash",
        log_dir=tmp_path,
        started_at=datetime(2026, 9, 10, 8, 0, 0),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["user_request"] == "请检查这家供应商"
    assert payload["material_id"] == "license_complete"
    assert payload["reference_date"] == "2026-09-01"
    assert payload["policy_version"] == "1.0.0"


# --- 评审二轮: ask_user 追问通道与异常边界 ---------------------------------------


def test_punctuation_bypass_now_requires_ask_user(policy) -> None:
    """评审复现句: 带问号的结论性纯文本不得放行, 只能经纠正转向 ask_user."""

    client = FakeClient(
        [
            text_response("材料齐全, 供应商已获批准。还有问题吗?"),
            ask_user_response("请问您需要检查哪家供应商的准入材料?"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert outcome.model_requests == 2
    correction = client.completions.calls[1]["messages"][-1]
    assert correction["role"] == "user"
    assert "ask_user" in correction["content"]


def test_ask_user_recorded_and_ends_run(policy) -> None:
    """合法 ask_user: 入事件流水, kind=question, 一次请求即结束."""

    client = FakeClient([ask_user_response("请问要检查哪家供应商?")])

    outcome = run_review(
        "帮我看看",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert outcome.model_requests == 1
    assert outcome.tool_attempts == 1
    event = outcome.tool_events[0]
    assert event["name"] == "ask_user"
    assert event["status"] == "ok"
    assert event["detail"]["question"] == "请问要检查哪家供应商?"


def test_model_request_exposes_the_four_tools(policy) -> None:
    """真实模型请求声明读材料, 校验, 检索与追问四个工具, 否则模型无从选择出口."""

    client = FakeClient([ask_user_response("请补充缺失材料的来源定位.")])

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    tool_names = [tool["function"]["name"] for tool in client.completions.calls[0]["tools"]]
    assert tool_names == ["read_material", "check_materials", "search_policy", "ask_user"]


def test_check_tool_accepts_declared_date_not_status(policy) -> None:
    """工具声明里只有原始事实: 有效期截止日与清单状态, 没有执照状态枚举."""

    client = FakeClient([ask_user_response("请先确认材料是否齐全.")])

    run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    check_tool = client.completions.calls[0]["tools"][1]["function"]
    properties = check_tool["parameters"]["properties"]
    assert "business_license_valid_until" in properties
    assert "business_license_document_status" not in properties


def test_ask_user_empty_question_uses_correction_budget(policy) -> None:
    """question 为空属参数错误: 走共享纠正预算, 改正后正常结束."""

    client = FakeClient(
        [
            ask_user_response("", call_id="call_ask_bad"),
            ask_user_response("需要检查的是哪家公司?", call_id="call_ask_ok"),
        ]
    )

    outcome = run_review(
        "帮我看看",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert [event["status"] for event in outcome.tool_events] == ["error", "ok"]
    # ask_user 参数错误时必须留下同轮正文, 否则无法区分"没想问"与"问在content里"
    assert outcome.tool_events[0]["assistant_content"] == ""


def test_ask_param_correction_is_bucketed_from_text_correction(policy) -> None:
    """真实日志复盘入测: 文本纠正与 ask_user 参数错误分属两桶, 不得互相残杀."""

    client = FakeClient(
        [
            text_response("请问需要检查哪家供应商?"),
            ask_user_response("", call_id="call_ask_bad"),
            ask_user_response("贵司名称是什么?", call_id="call_ask_ok"),
        ]
    )

    outcome = run_review(
        "帮我看看",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert outcome.model_requests == 3
    text_event, ask_event, ok_event = outcome.tool_events
    assert text_event["name"] == "<text-correction>"
    assert "请问需要检查哪家供应商?" in text_event["assistant_content"]
    assert [ask_event["status"], ok_event["status"]] == ["error", "ok"]


def test_multiple_calls_in_one_response_rejected(policy) -> None:
    """一次响应携带多个工具调用: 整体拒绝, 每个调用仍计数, 纠正后可继续."""

    dual = ChatCompletionMessage(
        role="assistant",
        content="",
        tool_calls=[
            ChatCompletionMessageToolCall(
                id="call_d1",
                type="function",
                function=Function(name="ask_user", arguments=json.dumps({"question": "问题一?"})),
            ),
            ChatCompletionMessageToolCall(
                id="call_d2",
                type="function",
                function=Function(name="ask_user", arguments=json.dumps({"question": "问题二?"})),
            ),
        ],
    )
    client = FakeClient(
        [_completion(dual), ask_user_response("一次只问一个: 需要检查哪家?", call_id="call_ask_ok")]
    )

    outcome = run_review(
        "帮我看看",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert outcome.tool_attempts == 3  # 双调用各计一次 + 纠正后的合法调用
    assert [event["status"] for event in outcome.tool_events] == ["error", "error", "ok"]


def test_ask_user_after_check_keeps_verified_result(policy) -> None:
    """校验成功后仍可追问: kind=question 但已完成的校验结果保留在流水里."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments()),
            ask_user_response("还需要补充其他材料信息吗?", call_id="call_ask_ok"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    check_event = outcome.tool_events[1]
    assert check_event["name"] == "check_materials"
    assert check_event["status"] == "ok"
    assert [e["result"] for e in check_event["detail"]["evaluations"]] == ["not_hit", "not_hit"]


def test_missing_facts_require_ask_user_after_check(policy) -> None:
    """校验发现缺失事实后, 带追问的纯文本必须纠正为 ask_user 调用."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(docs_complete=None)),
            text_response("当前无法判断材料是否齐全。请问品类必填材料是否齐全?"),
            ask_user_response("请问品类必填材料是否齐全?", call_id="call_ask_ok"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "question"
    assert outcome.model_requests == 4
    assert [event["name"] for event in outcome.tool_events] == [
        "read_material",
        "check_materials",
        "<text-correction>",
        "ask_user",
    ]


def test_missing_facts_block_answer_without_question(policy) -> None:
    """缺字段时连"看似完整"的结论也不放行: 只能追问, 不能收尾."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(docs_complete=None)),
            text_response("材料已检查完毕, 两条规则均未命中, 建议通过准入."),
            text_response("材料已检查完毕, 建议通过准入."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert outcome.model_requests == 4


def test_empty_choices_fails_without_crash(policy) -> None:
    """响应 choices 为空(如内容过滤)不得 IndexError, 必须走明确失败."""

    empty = ChatCompletion(
        id="chatcmpl-empty",
        choices=[],
        created=0,
        model="test-fake",
        object="chat.completion",
    )
    client = FakeClient([empty])

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert "choices" in outcome.text


def test_tool_internal_error_fails_cleanly(policy, monkeypatch) -> None:
    """工具执行抛非参数异常: 记录错误事件并明确失败, 不裸崩."""

    def boom(*args, **kwargs):
        raise RuntimeError("意外内部故障")

    monkeypatch.setattr("vendorguard.agent.check_extracted_facts", boom)
    client = FakeClient([read_response(), tool_call_response(check_arguments())])

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert "内部错误" in outcome.text
    assert outcome.tool_events[-1]["status"] == "error"


def test_stale_tool_response_not_executed(policy) -> None:
    """响应迟到越过总时限: 工具不得执行, 事件流水保持为空并报告超时."""

    scripted = read_response()

    class SlowCompletions:
        def create(self, **kwargs):
            time.sleep(0.05)
            return scripted

    client = SimpleNamespace(chat=SimpleNamespace(completions=SlowCompletions()))

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        total_timeout=0.01,
    )

    assert outcome.kind == "failed"
    assert "时限" in outcome.text
    assert outcome.tool_events == []


def test_write_run_log_redacts_hyphen_underscore_keys(tmp_path) -> None:
    """脱敏正则必须覆盖带连字符与下划线的密钥样式."""

    path = write_run_log(
        make_outcome(text="模型请求失败: sk-abc_def-9876543210 leaked", kind="failed"),
        model_name="qwen3.7-flash",
        log_dir=tmp_path,
        started_at=datetime(2026, 9, 10, 12, 0, 0),
    )

    written = path.read_text(encoding="utf-8")
    assert "sk-abc_def-9876543210" not in written
    assert "sk-***" in written


def test_supplement_round_resolves_missing_fact(policy) -> None:
    """两轮闭环: 第一轮缺字段追问, 登记补充后第二轮引用补充来源并给出结论。"""

    session = ReviewSession.start(material())
    first_client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(valid_until=None)),
            text_response("材料里没有有效期截止日, 需要向用户追问。"),
            ask_user_response("请提供营业执照的有效期截止日?", call_id="call_ask_ok"),
        ]
    )

    first = run_review(
        "请检查",
        session=session,
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=first_client,
    )

    assert first.kind == "question"
    assert first.supplement_rounds == 0
    assert first.tool_events[-1]["name"] == "ask_user"

    session.record_supplement("补充说明: 营业执照有效期至 2030年01月31日")
    second_client = FakeClient(
        [
            read_response(),
            tool_call_response(
                check_arguments(valid_until="2030-01-31", date_source="user_supplement@round:1")
            ),
            text_response("根据工具结果: 两条规则均为 not_hit."),
        ]
    )

    second = run_review(
        "请检查",
        session=session,
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=second_client,
    )

    assert second.kind == "answer"
    assert second.supplement_rounds == 1
    # 第二轮开场消息必须带上补充原文与它的来源定位, 否则模型无从引用
    opening = second_client.completions.calls[0]["messages"][1]["content"]
    assert "user_supplement@round:1" in opening
    assert "2030年01月31日" in opening
    check_event = second.tool_events[1]
    assert check_event["detail"]["verified_sources"]["business_license_valid_until"] == (
        "user_supplement@round:1"
    )
    assert check_event["detail"]["derived_business_license_status"] == "valid"


def test_unregistered_supplement_round_is_rejected_in_loop(policy) -> None:
    """模型引用还没登记的补充轮次: 来源核对失败, 规则不执行。"""

    session = ReviewSession.start(material())
    client = FakeClient(
        [
            read_response(),
            tool_call_response(
                check_arguments(valid_until="2030-01-31", date_source="user_supplement@round:2")
            ),
            tool_call_response(
                check_arguments(valid_until="2030-01-31", date_source="user_supplement@round:2"),
                call_id="call_test_2",
            ),
        ]
    )

    outcome = run_review(
        "请检查",
        session=session,
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert outcome.tool_events[1]["status"] == "error"
    assert "user_supplement@round:2" in outcome.tool_events[1]["detail"]


# ---------------------------------------------------------------------------
# M3-4: search_policy 接进同一个循环
# ---------------------------------------------------------------------------


def test_search_round_trip_hands_citable_nodes_to_the_model(policy) -> None:
    """read -> check -> search -> 结论: 交给模型的是可引用节点, 定位与原文."""

    index = policy_index()
    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            search_response(call_id="call_search_1"),
            text_response("正常准入条件要求营业执照在参考日期仍有效; VEN-001 未命中."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        policy_index=index,
    )

    assert outcome.kind == "answer"
    assert [event["name"] for event in outcome.tool_events] == [
        "read_material",
        "check_materials",
        "search_policy",
    ]
    search_event = outcome.tool_events[2]
    assert search_event["status"] == "ok"
    assert search_event["detail"]["returned_nodes"] == [item.node_key for item in index.records]
    assert search_event["detail"]["query"] == "关键物料可以先准入后补交质量证书吗"
    assert search_event["detail"]["as_of"] == "2026-09-01"

    # 第 4 次请求 (下标 3) 才带上检索结果, 用它核对交给模型的字段
    payload = tool_payload(client, 3, "call_search_1")
    assert payload["scope"] == list(CURRENT_EDITION_KEYS)
    assert payload["as_of"] == "2026-09-01"
    first = payload["results"][0]
    assert set(first) == {"chunk_key", "node_key", "edition_key", "title", "locator", "text"}
    assert first["node_key"] == index.records[0].node_key
    assert first["chunk_key"] != first["node_key"]
    assert first["text"] == index.records[0].display_text
    assert first["title"] == "第1节"
    assert first["locator"] == ["第1节"]
    # 分数不给模型: 它跨请求不可比, 给出去只会被当成证据
    assert "score" not in json.dumps(payload)


def test_search_tool_declares_query_and_top_k(policy) -> None:
    """检索工具声明 query 必填, top_k 可选, 并写明引用只用 node_key."""

    client = FakeClient([ask_user_response("请补充缺失材料的来源定位.")])

    run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    search_tool = client.completions.calls[0]["tools"][2]["function"]
    assert search_tool["name"] == "search_policy"
    assert search_tool["parameters"]["required"] == ["query"]
    assert set(search_tool["parameters"]["properties"]) == {"query", "top_k"}
    assert "node_key" in search_tool["description"]


def test_search_argument_error_uses_the_shared_correction_channel(policy) -> None:
    """检索参数非法走共享纠正通道: 模型改正之后仍能继续走完链条."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            tool_call_response({"top_k": 3}, call_id="call_bad_search", name="search_policy"),
            search_response(call_id="call_search_ok"),
            text_response("依据检索到的现行条款作答."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        policy_index=policy_index(),
    )

    assert outcome.kind == "answer"
    rejected = outcome.tool_events[2]
    assert rejected["name"] == "search_policy"
    assert rejected["status"] == "error"
    assert "query" in rejected["detail"]
    assert outcome.tool_events[3]["status"] == "ok"


def test_top_k_out_of_range_is_offered_back_for_correction(policy) -> None:
    """top_k 越界由检索层判定, 作为参数错误回给模型而不是直接失败."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            search_response(top_k=9, call_id="call_bad_topk"),
            search_response(top_k=5, call_id="call_search_ok"),
            text_response("依据检索到的现行条款作答."),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        policy_index=policy_index(),
    )

    assert outcome.kind == "answer"
    rejected = outcome.tool_events[2]
    assert rejected["status"] == "error"
    assert "top_k" in rejected["detail"]


def test_search_without_loaded_index_fails_the_run(policy) -> None:
    """没有装载检索数据时调用检索工具直接失败, 不产出结论."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            search_response(call_id="call_search_1"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
    )

    assert outcome.kind == "failed"
    assert "检索" in outcome.text
    assert outcome.tool_events[-1]["name"] == "search_policy"
    assert outcome.tool_events[-1]["status"] == "error"


def test_embedding_failure_fails_the_run_without_answer(policy) -> None:
    """embedding 不可用属于检索失败: 直接失败, 不给纠正机会也不继续要结论."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            search_response(call_id="call_search_1"),
            text_response("这条结论不应该被请求到"),
        ],
        embeddings=FakeEmbeddings(error=RuntimeError("connection reset")),
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        policy_index=policy_index(),
    )

    assert outcome.kind == "failed"
    assert "检索" in outcome.text
    assert outcome.tool_events[-1]["status"] == "error"
    # 剧本里还留着一条结论响应: 没有继续请求它, 说明失败是立即发生的
    assert client.completions.scripted == [text_response("这条结论不应该被请求到")]


def test_search_attempts_count_toward_the_tool_budget(policy) -> None:
    """检索与其它工具共用同一份工具调用预算."""

    client = FakeClient(
        [
            read_response(),
            tool_call_response(check_arguments(), call_id="call_check_1"),
            search_response(call_id="call_search_1"),
            search_response(call_id="call_search_2"),
        ]
    )

    outcome = run_review(
        "请检查",
        session=ReviewSession.start(material()),
        reference_date=REFERENCE_DATE,
        policy=policy,
        client=client,
        policy_index=policy_index(),
        max_tool_attempts=3,
    )

    assert outcome.kind == "failed"
    assert "上限" in outcome.text
    assert outcome.tool_attempts == 3


def test_load_policy_index_uses_the_committed_chunk_list(tmp_path) -> None:
    """启动装载读的是入库清单: 22 条现行片段, 向量等长同维, 并把缓存落到指定位置."""

    cache_path = tmp_path / "cache" / "embedding_v1.json"

    index = load_policy_index(
        client=FakeClient([]),
        model="qwen3.7-text-embedding",
        cache_path=cache_path,
        chunk_list_path=CHUNK_LIST_PATH,
    )

    assert len(index.records) == 22
    assert len(index.vectors.vectors) == 22
    assert index.vectors.dimension == EMBEDDING_DIMENSION
    assert all(record.edition_key in CURRENT_EDITION_KEYS for record in index.records)
    assert cache_path.is_file()


def test_load_policy_index_reports_a_missing_chunk_list(tmp_path) -> None:
    """清单缺失归一为启动错误, 而不是给出一份空索引."""

    with pytest.raises(AgentConfigError, match="清单"):
        load_policy_index(
            client=FakeClient([]),
            model="qwen3.7-text-embedding",
            cache_path=tmp_path / "embedding_v1.json",
            chunk_list_path=tmp_path / "missing.jsonl",
        )
