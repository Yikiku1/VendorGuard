"""审查完整链路的集成式单元测试, 使用剧本化假模型客户端.

这些测试不访问网络: 模型响应由剧本给定, 因此可以精确断言"程序在模型
不配合时会怎么做", 这正是本项目要保证的部分。
"""

from __future__ import annotations

import json
from datetime import date
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

from vendorguard.agent_session import ReviewSession
from vendorguard.materials import MaterialText, SourceTexts
from vendorguard.policy import load_policy
from vendorguard.retrieval import load_knowledge_index
from vendorguard.review import find_approval_claims, run_review, verify_citation_labels

REFERENCE_DATE = date(2026, 9, 1)

LICENSE_PAGE = (
    "营业执照(演示样例, 非真实证照)\n"
    "企业名称: 演示供应商有限公司\n"
    "声明有效期至: 2027-08-31\n"
    "材料清单已齐备"
)


def _completion(message: ChatCompletionMessage, finish_reason: str = "stop") -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-test",
        choices=[Choice(finish_reason=finish_reason, index=0, message=message)],
        created=0,
        model="test-fake",
        object="chat.completion",
    )


def tool_call(name: str, arguments: object, *, call_id: str = "call_1") -> ChatCompletion:
    """剧本: 模型调用指定工具, arguments 传 str 时原样作为参数文本."""

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
    """剧本: 模型直接给文字, 在本链路里不能作为结论出口."""

    return _completion(ChatCompletionMessage(role="assistant", content=text), finish_reason)


class FakeCompletions:
    """按剧本供给响应, 并记录收到的请求参数."""

    def __init__(self, scripted: list[ChatCompletion]) -> None:
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def create(self, **kwargs) -> ChatCompletion:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        assert self.scripted, "模型请求次数超出剧本, 循环可能存在预算泄漏"
        return self.scripted.pop(0)


class FakeClient:
    """伪装 openai.OpenAI 中循环实际用到的那一小块接口."""

    def __init__(self, scripted: list[ChatCompletion]) -> None:
        self.completions = FakeCompletions(scripted)
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.fixture(scope="module")
def policy():
    return load_policy(Path("policies/rules/v1.0.0.yaml"))


@pytest.fixture(scope="module")
def index():
    return load_knowledge_index(Path("data/knowledge"))


def _session(pages: tuple[str, ...] = (LICENSE_PAGE,)) -> ReviewSession:
    return ReviewSession(
        sources=SourceTexts.from_materials(
            [
                MaterialText(
                    material_id="license_normal",
                    source_name="license_normal.pdf",
                    sha256="d" * 64,
                    page_count=len(pages),
                    pages=pages,
                )
            ]
        )
    )


def _run(client: FakeClient, session: ReviewSession, policy, index, **kwargs):
    return run_review(
        "请审查这份供应商准入材料。",
        session=session,
        policy=policy,
        index=index,
        client=client,
        model_name="test-fake",
        reference_date=REFERENCE_DATE,
        **kwargs,
    )


def test_happy_path_produces_report_with_program_rendered_rule_results(policy, index) -> None:
    """正常链路: 读材料, 校验通过, 检索, 提交报告; 规则与引用由程序渲染."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "category_required_documents_complete": True,
                    "sources": {
                        "business_license_declared_valid_until": "license_normal@page:1",
                        "category_required_documents_complete": "license_normal@page:1",
                    },
                },
            ),
            tool_call("search_policy", {"query": "供应商准入 营业执照 材料"}),
            tool_call(
                "submit_report",
                {
                    "summary": "材料齐备, 营业执照在有效期内, 两条规则均未命中。",
                    "open_items": ["证书真实性需人工核验"],
                },
            ),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    assert outcome.kind == "report"
    assert "# 供应商材料初审报告" in outcome.text
    # 规则结果与状态派生由程序写入报告, 不依赖模型叙述
    assert "| VEN-001 | not_hit |" in outcome.text
    assert "营业执照状态由程序按参考日期派生: valid" in outcome.text
    assert "不构成供应商准入批准" in outcome.text
    assert outcome.rule_results[0]["rule_id"] == "VEN-001"


def test_ask_user_returns_question_without_report(policy, index) -> None:
    """材料读不到日期时走追问通道, 返回 question 而不是编造结论."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_missing_date"}),
            tool_call(
                "check_materials",
                {
                    "category_required_documents_complete": True,
                    "sources": {
                        "category_required_documents_complete": "license_missing_date@page:1"
                    },
                },
            ),
            tool_call("ask_user", {"prompt": "请提供营业执照的声明有效期截止日。"}),
        ]
    )
    session = ReviewSession(
        sources=SourceTexts.from_materials(
            [
                MaterialText(
                    material_id="license_missing_date",
                    source_name="m.pdf",
                    sha256="e" * 64,
                    page_count=1,
                    pages=("企业名称: 演示供应商有限公司\n材料清单已齐备",),
                )
            ]
        )
    )

    outcome = _run(client, session, policy, index)

    assert outcome.kind == "question"
    assert "声明有效期" in outcome.text


def test_report_is_refused_while_facts_missing(policy, index) -> None:
    """仍有缺失字段时直接提交报告会被拒绝并要求先追问."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_missing_date"}),
            tool_call(
                "check_materials",
                {
                    "sources": {},
                },
            ),
            tool_call("submit_report", {"summary": "看起来没问题", "open_items": []}),
            tool_call("ask_user", {"prompt": "请提供营业执照的声明有效期截止日。"}),
        ]
    )
    session = ReviewSession(
        sources=SourceTexts.from_materials(
            [
                MaterialText(
                    material_id="license_missing_date",
                    source_name="m.pdf",
                    sha256="e" * 64,
                    page_count=1,
                    pages=("企业名称: 演示供应商有限公司",),
                )
            ]
        )
    )

    outcome = _run(client, session, policy, index)

    assert outcome.kind == "question"
    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("缺失字段" in str(event["detail"]) for event in errors)


def test_plain_text_cannot_be_conclusion(policy, index) -> None:
    """纯文本不是结论出口: 首次违规被纠正, 纠正后再犯即失败."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            text_response("我认为材料合格。"),
            text_response("我认为材料合格。"),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    assert outcome.kind == "failed"
    assert "纠正机会已用尽" in outcome.text


def test_forged_citation_is_rejected(policy, index) -> None:
    """报告里编造检索结果之外的出处会被拒绝."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "category_required_documents_complete": True,
                    "sources": {
                        "business_license_declared_valid_until": "license_normal@page:1",
                        "category_required_documents_complete": "license_normal@page:1",
                    },
                },
            ),
            tool_call("search_policy", {"query": "营业执照 有效期"}),
            tool_call(
                "submit_report",
                {
                    "summary": "依据 totally_fake_policy@第九十九条 判断材料合格。",
                    "open_items": [],
                },
            ),
            # 被拒绝后模型纠正为不带编造出处的表述
            tool_call(
                "submit_report",
                {
                    "summary": "材料齐备, 营业执照在有效期内, 规则均未命中。",
                    "open_items": [],
                },
            ),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("不得编造出处" in str(event["detail"]) for event in errors)
    assert outcome.kind == "report"
    assert "totally_fake_policy" not in outcome.text


def test_fabricated_date_is_rejected_by_literal_check(policy, index) -> None:
    """模型读数不在材料原文中时被拦截, 规则不会执行."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2030-01-01",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            tool_call("ask_user", {"prompt": "请提供营业执照的声明有效期截止日。"}),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("未出现在所声明的来源" in str(event["detail"]) for event in errors)
    assert outcome.rule_results == []


def test_unknown_material_is_refused(policy, index) -> None:
    """模型申请读取本次未提交的材料时被拒绝, 白名单之外无访问."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "someone_elses_file"}),
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            tool_call(
                "submit_report",
                {"summary": "材料合格。", "open_items": []},
            ),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("本次提交中没有材料" in str(event["detail"]) for event in errors)


def test_report_before_check_is_refused(policy, index) -> None:
    """未执行校验就提交报告会被拒绝."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call("submit_report", {"summary": "直接给结论", "open_items": []}),
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            tool_call("submit_report", {"summary": "材料合格。", "open_items": []}),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("先执行 check_materials" in str(event["detail"]) for event in errors)


def test_check_before_read_is_refused(policy, index) -> None:
    """未读材料就校验会被拒绝, 强制"先读原文再报事实"的顺序."""

    client = FakeClient(
        [
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            tool_call("submit_report", {"summary": "材料合格。", "open_items": []}),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("必须先调用 read_material" in str(event["detail"]) for event in errors)


def test_model_request_budget_is_enforced(policy, index) -> None:
    """模型请求达到预算上限时明确失败, 不静默降级."""

    repeated = [tool_call("read_material", {"material_id": "license_normal"})] * 4
    client = FakeClient(repeated)

    outcome = _run(client, _session(), policy, index, max_model_requests=3)

    assert outcome.kind == "failed"
    assert "上限" in outcome.text


def test_unknown_tool_is_refused_then_corrected(policy, index) -> None:
    """未知工具被拒绝并消耗纠正机会, 纠正后仍可继续正常流程。

    纠正机会是共享的一次, 因此剧本里不再安排其他会被拒绝的调用。
    """

    client = FakeClient(
        [
            tool_call("delete_everything", {}),
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "category_required_documents_complete": True,
                    "sources": {
                        "business_license_declared_valid_until": "license_normal@page:1",
                        "category_required_documents_complete": "license_normal@page:1",
                    },
                },
            ),
            tool_call("submit_report", {"summary": "材料合格。", "open_items": []}),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("未知工具" in str(event["detail"]) for event in errors)
    assert outcome.kind == "report"


def test_supplement_can_replace_missing_fact(policy, index) -> None:
    """用户补充可提供材料里读不到的事实, 来源标为程序登记的用户补充轮次。

    演示两轮: 第一轮材料缺日期而追问, 用户补充后第二轮继续并给出报告。
    """

    session = ReviewSession(
        sources=SourceTexts.from_materials(
            [
                MaterialText(
                    material_id="license_missing_date",
                    source_name="m.pdf",
                    sha256="f" * 64,
                    page_count=1,
                    pages=("企业名称: 演示供应商有限公司",),
                )
            ]
        )
    )
    # 第一轮: 材料里读不到声明有效期, 走追问。轮次由 run_review 内部推进
    first_round = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_missing_date"}),
            tool_call(
                "check_materials",
                {
                    "category_required_documents_complete": True,
                    "sources": {
                        "category_required_documents_complete": "license_missing_date@page:1"
                    },
                },
            ),
            tool_call("ask_user", {"prompt": "请提供营业执照的声明有效期截止日。"}),
        ]
    )
    asked = _run(first_round, session, policy, index)
    assert asked.kind == "question"
    assert session.round_number == 1

    # 用户补充登记在第 1 轮, 下一次 run_review 自动进入第 2 轮
    session.record_supplement("营业执照声明有效期至 2028-12-31")

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_missing_date"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2028-12-31",
                    "category_required_documents_complete": True,
                    "sources": {
                        "business_license_declared_valid_until": "用户补充@R1",
                        "category_required_documents_complete": "license_missing_date@page:1",
                    },
                },
            ),
            tool_call("search_policy", {"query": "营业执照 有效期 准入"}),
            tool_call("submit_report", {"summary": "补充后材料齐备。", "open_items": []}),
        ]
    )

    outcome = _run(client, session, policy, index)

    assert outcome.kind == "report"
    assert outcome.rounds == 2
    assert "营业执照状态由程序按参考日期派生: valid" in outcome.text


def test_session_round_limit_stops_run(policy, index) -> None:
    """会话轮次达到上限后不再开始新一轮。

    `run_review` 自己推进轮次, 因此这里先把轮次推到上限, 再确认下一次
    调用被 SessionError 拒绝, 而不是无界往复。
    """

    from vendorguard.agent_session import SessionError

    session = _session()
    session.max_rounds = 1
    session.begin_round()

    with pytest.raises(SessionError):
        _run(FakeClient([]), session, policy, index)


def test_outcome_records_observability_fields(policy, index) -> None:
    """结果对象带上运行观测所需字段, 供脱敏记录落盘."""

    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "sources": {"business_license_declared_valid_until": "license_normal@page:1"},
                },
            ),
            tool_call("search_policy", {"query": "营业执照"}),
            tool_call("submit_report", {"summary": "合格。", "open_items": []}),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    assert outcome.material_ids == ["license_normal"]
    assert outcome.reference_date == REFERENCE_DATE
    assert outcome.policy_version == "1.0.0"
    assert outcome.model_requests > 0
    assert outcome.tool_attempts >= 4
    assert outcome.elapsed_seconds >= 0
    assert outcome.citations, "检索命中应被记录, 使报告出处可复核"


def test_verify_citation_labels_extracts_labels() -> None:
    """引用标签核对能识别被全角标点紧邻的标签, 不误判为伪造."""

    allowed = {"a_policy@正常准入条件"}
    text = "依据 a_policy@正常准入条件\uff08现行\uff09判断。"

    assert verify_citation_labels(text, allowed=allowed) == []


def test_verify_citation_labels_reports_forgeries() -> None:
    """不在检索结果中的标签会被列出."""

    forged = verify_citation_labels("依据 made_up@第九条 判断。", allowed=set())
    assert forged == ["made_up@第九条"]


def test_verify_citation_labels_allows_program_sources() -> None:
    """材料页与用户补充不是制度出处, 引用它们不算编造。

    实测中曾把 `材料ID@page:1` 判为伪造, 连续拒绝两次导致整次运行失败。
    这两类来源由程序掌握(材料页经字面核对, 补充轮次由程序登记), 模型造不出
    不存在的页或轮次, 因此按来源形态放行。
    """

    allowed = {"demo_policy_v2@正常准入条件"}

    assert verify_citation_labels("材料 license_normal@page:1 显示有效。", allowed=allowed) == []
    assert verify_citation_labels("依据 用户补充@R1 的信息。", allowed=allowed) == []
    # 伪造的制度出处仍必须被拦
    assert verify_citation_labels("依据 fake@第九条。", allowed=allowed) == ["fake@第九条"]


def test_verify_citation_labels_rejects_fake_page_number_shape() -> None:
    """材料来源必须带合法页码; 形似的伪来源不当成材料放行。"""

    allowed: set[str] = set()

    # 没有页码, 不符合 材料ID@page:N 形态, 仍按未知标签拦下
    assert verify_citation_labels("依据 license@page 判断。", allowed=allowed) == ["license@page"]


def test_find_approval_claims_detects_overreach() -> None:
    """叙述里宣告准入结论的措辞会被检出."""

    assert find_approval_claims("初审建议, 准入通过。") == ["准入通过"]
    assert find_approval_claims("因此建议批准该供应商准入。") == ["建议批准"]
    assert find_approval_claims("可以准予准入") == ["准予准入"]


def test_find_approval_claims_ignores_neutral_wording() -> None:
    """中性描述与免责声明都不该被误报, 否则正常报告会被反复拒绝."""

    assert find_approval_claims("两条规则均未命中, 材料齐备。") == []
    # 报告结尾的免责声明是必需的, 不能因为它出现而拒绝成稿
    assert find_approval_claims("本报告为初审建议, 不构成供应商准入批准。") == []
    # 相反结论同样不是越权宣告
    assert find_approval_claims("该供应商未通过准入审查。") == []


def test_find_approval_claims_not_fooled_by_unrelated_negation() -> None:
    """句中其他位置的否定不能掩盖真正的越权结论."""

    claims = find_approval_claims("材料不齐全, 建议批准准入。")
    # "建议批准"与"批准准入"都命中, 关键是结论没有被误判为已否定
    assert "建议批准" in claims
    assert "批准准入" in claims


def test_report_narrative_cannot_declare_approval(policy, index) -> None:
    """模型在报告叙述里宣告准入结论时被拒绝并要求改写。

    提示词已要求不得宣告结论, 但只靠提示词约束不住——这与 M1 的结论
    出口问题同源。这里验证机械检查确实挡住了越权表述。
    """

    overreach = "初审建议, 准入通过。"
    corrected = "两条规则均未命中, 材料齐备, 未发现需要处置的事项。"
    client = FakeClient(
        [
            tool_call("read_material", {"material_id": "license_normal"}),
            tool_call(
                "check_materials",
                {
                    "business_license_declared_valid_until": "2027-08-31",
                    "category_required_documents_complete": True,
                    "sources": {
                        "business_license_declared_valid_until": "license_normal@page:1",
                        "category_required_documents_complete": "license_normal@page:1",
                    },
                },
            ),
            tool_call("search_policy", {"query": "营业执照 准入 有效期"}),
            tool_call("submit_report", {"summary": overreach, "open_items": []}),
            tool_call("submit_report", {"summary": corrected, "open_items": []}),
        ]
    )

    outcome = _run(client, _session(), policy, index)

    errors = [event for event in outcome.tool_events if event["status"] == "error"]
    assert any("不得宣告准入结论" in str(event["detail"]) for event in errors)
    assert outcome.kind == "report"
    assert "准入通过" not in outcome.text
    assert corrected in outcome.text
