"""测试提取事实管线: 来源核对, 状态派生与规则执行。

契约由本测试固定: agent_tools 只提供 parse_extracted_facts 与
check_extracted_facts 这一条管线。它接收模型从材料里读出的原始事实, 先核对
来源再派生状态; 来源不存在、事实与原文不一致或模型想直接提交结论时, 一律抛
ToolArgumentError 且不执行任何规则。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vendorguard import agent_tools
from vendorguard.agent_tools import (
    ExtractedCheckResult,
    ToolArgumentError,
    check_extracted_facts,
    parse_extracted_facts,
)
from vendorguard.materials import MaterialSources, read_text_pdf
from vendorguard.policy import PolicyDocument, RuleEvaluation, load_policy

RULES_PATH = Path("policies/rules/v1.0.0.yaml")
MATERIALS_DIR = Path(__file__).resolve().parents[2] / "data" / "demo" / "materials"
# 与 data/demo/cases/*.yaml 的 reference_date 一致, 派生状态以它为基准。
REFERENCE_DATE = date(2026, 9, 1)


@pytest.fixture(scope="module")
def policy() -> PolicyDocument:
    """加载现行规则配置, 本期只启用 VEN-001 与 VEN-002。"""

    return load_policy(RULES_PATH)


def as_map(result: ExtractedCheckResult) -> dict[str, RuleEvaluation]:
    """按规则 ID 索引执行结果, 方便逐条断言。"""

    return {evaluation.rule_id: evaluation for evaluation in result.evaluations}


# --- 提取事实管线: 来源核对、状态派生与规则执行 -------------------------------


@pytest.fixture
def sources() -> MaterialSources:
    """每次测试都用全新的来源集合, 避免补充轮次在用例之间互相污染。"""

    return MaterialSources.from_materials([read_text_pdf(MATERIALS_DIR / "license_complete.pdf")])


def extraction_payload(
    *,
    valid_until: str | None = "2027-08-31",
    docs_complete: bool | None = True,
    date_source: str | None = "license_complete@page:1",
    checklist_source: str | None = "license_complete@page:1",
) -> str:
    """拼出模型提交的工具参数 JSON; 传 None 表示该事实缺失, 来源一并省略。"""

    payload: dict[str, object] = {}
    declared_sources: dict[str, str] = {}
    if valid_until is not None:
        payload["business_license_valid_until"] = valid_until
        if date_source is not None:
            declared_sources["business_license_valid_until"] = date_source
    if docs_complete is not None:
        payload["category_required_documents_complete"] = docs_complete
        if checklist_source is not None:
            declared_sources["category_required_documents_complete"] = checklist_source
    payload["sources"] = declared_sources
    return json.dumps(payload, ensure_ascii=False)


def run_pipeline(
    policy: PolicyDocument,
    sources: MaterialSources,
    payload: str,
    *,
    reference_date: date = REFERENCE_DATE,
):
    """按循环里的用法跑完整条管线: 先解析参数, 再核对来源并执行规则。"""

    facts = parse_extracted_facts(payload)
    return check_extracted_facts(
        facts, sources=sources, policy=policy, reference_date=reference_date
    )


def test_complete_material_passes_with_verified_sources(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """完整材料: 两条规则均不命中, 来源逐条核实, 没有缺字段。"""

    result = run_pipeline(policy, sources, extraction_payload())

    evaluations = as_map(result)  # type: ignore[arg-type]
    assert evaluations["VEN-001"].result == "not_hit"
    assert evaluations["VEN-002"].result == "not_hit"
    assert result.missing_fields == []
    assert result.policy_version == policy.version
    assert result.verified_sources == {
        "business_license_valid_until": "license_complete@page:1",
        "category_required_documents_complete": "license_complete@page:1",
    }


@pytest.mark.parametrize(
    ("reference_date", "expected_status", "expected_ven001"),
    [(date(2027, 8, 31), "valid", "not_hit"), (date(2027, 9, 1), "expired", "hit")],
    ids=["到期日当天仍有效", "到期次日算过期"],
)
def test_derived_status_and_rule_follow_reference_date(
    policy: PolicyDocument,
    sources: MaterialSources,
    reference_date: date,
    expected_status: str,
    expected_ven001: str,
) -> None:
    """执照状态由程序按参考日期派生: 同一天材料, 换个参考日期结论就相反。"""

    result = run_pipeline(policy, sources, extraction_payload(), reference_date=reference_date)

    assert result.derived_business_license_status == expected_status
    ven001 = as_map(result)["VEN-001"]  # type: ignore[arg-type]
    assert ven001.result == expected_ven001
    if expected_status == "expired":
        assert ven001.outcome is not None
        assert ven001.outcome.reason_code == "business_license_expired"


def test_chinese_date_writing_matches_ascii_source(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """模型写 2027年8月31日 也能与材料里的 2027-08-31 对上: 比较归一化年月日。"""

    result = run_pipeline(policy, sources, extraction_payload(valid_until="2027年8月31日"))

    assert result.derived_business_license_status == "valid"


def test_fabricated_date_is_rejected_before_rules(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """模型编一个材料里没有的日期: 来源核对失败, 规则不执行。"""

    with pytest.raises(ToolArgumentError, match="business_license_valid_until"):
        run_pipeline(policy, sources, extraction_payload(valid_until="2030-01-01"))


@pytest.mark.parametrize(
    "locator",
    ["license_complete@page:2", "license_complete@page:0", "other_material@page:1"],
    ids=["页码越界", "页码非法", "材料未登记"],
)
def test_fabricated_source_locator_is_rejected(
    policy: PolicyDocument, sources: MaterialSources, locator: str
) -> None:
    """伪造的页码或材料 ID 都取不到原文: 来源不存在, 规则不执行。"""

    with pytest.raises(ToolArgumentError, match="business_license_valid_until"):
        run_pipeline(policy, sources, extraction_payload(date_source=locator))


def test_opposite_checklist_claim_is_rejected(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """材料写着"齐全"而模型声明 False: 清单文字不一致, 规则不执行。"""

    with pytest.raises(ToolArgumentError, match="category_required_documents_complete"):
        run_pipeline(policy, sources, extraction_payload(docs_complete=False))


def test_supplement_round_must_be_registered(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """未登记的补充轮次不能当来源; 程序登记之后, 同一份参数才核对通过。"""

    payload = extraction_payload(valid_until="2028-05-20", date_source="user_supplement@round:1")

    with pytest.raises(ToolArgumentError, match="business_license_valid_until"):
        run_pipeline(policy, sources, payload)

    sources.add_supplement(1, "补充说明: 营业执照有效期至 2028年05月20日")
    result = run_pipeline(policy, sources, payload)

    assert result.derived_business_license_status == "valid"
    assert result.verified_sources["business_license_valid_until"] == "user_supplement@round:1"


def test_missing_date_reports_field_without_deriving_status(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """模型没读到有效期: 不派生状态, 缺字段如实报告, VEN-001 走缺失输入处置。"""

    result = run_pipeline(policy, sources, extraction_payload(valid_until=None))

    assert result.derived_business_license_status is None
    assert result.missing_fields == ["business_license_valid_until"]
    ven001 = as_map(result)["VEN-001"]  # type: ignore[arg-type]
    assert ven001.outcome is not None
    assert ven001.outcome.reason_code == "business_license_missing"


def test_model_cannot_submit_derived_license_status(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """模型直接提交执照状态结论(而不是原始日期): extra=forbid 拦在参数层。"""

    payload = json.dumps(
        {
            "business_license_document_status": "valid",
            "sources": {"business_license_document_status": "license_complete@page:1"},
        }
    )

    with pytest.raises(ToolArgumentError):
        parse_extracted_facts(payload)


def test_declared_fact_without_source_is_rejected() -> None:
    """声明了事实却不给来源: 参数层直接拒绝, 不替模型找来源。"""

    payload = json.dumps({"business_license_valid_until": "2027-08-31"})

    with pytest.raises(ToolArgumentError, match="business_license_valid_until"):
        parse_extracted_facts(payload)


def test_rules_are_not_evaluated_when_source_check_fails(
    policy: PolicyDocument, sources: MaterialSources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """来源核对失败时规则执行器一次都没被调用: 用替身直接证实, 不靠推断。"""

    calls: list[str] = []
    monkeypatch.setattr(agent_tools, "evaluate_rule", lambda rule, **kwargs: calls.append(rule.id))

    with pytest.raises(ToolArgumentError):
        run_pipeline(policy, sources, extraction_payload(valid_until="2030-01-01"))

    assert calls == []


def test_incomplete_checklist_hits_ven002(policy: PolicyDocument, sources: MaterialSources) -> None:
    """清单不齐全且有原文支撑时 VEN-002 命中, 处置为补件建议。"""

    sources.add_supplement(1, "补充说明: 材料清单状态: 不齐全, 缺少质量证书")
    payload = extraction_payload(docs_complete=False, checklist_source="user_supplement@round:1")

    result = run_pipeline(policy, sources, payload)

    ven002 = as_map(result)["VEN-002"]
    assert ven002.result == "hit"
    assert ven002.outcome is not None
    assert ven002.outcome.action == "request_documents"
    assert ven002.outcome.reason_code == "category_required_documents_missing"
    assert result.missing_fields == []


def test_unknown_completeness_uses_missing_input_outcome(
    policy: PolicyDocument, sources: MaterialSources
) -> None:
    """清单完整性未知时字段键必须缺席, VEN-002 走缺失输入处置而非 not_hit。"""

    result = run_pipeline(policy, sources, extraction_payload(docs_complete=None))

    ven002 = as_map(result)["VEN-002"]
    assert ven002.result == "hit"
    assert ven002.outcome is not None
    assert ven002.outcome.action == "create_manual_review"
    assert result.missing_fields == ["category_required_documents_complete"]


def test_parse_extracted_facts_rejects_bad_json() -> None:
    """非 JSON 文本归一为参数错误, 不向外抛 json.JSONDecodeError。"""

    with pytest.raises(ToolArgumentError, match="JSON"):
        parse_extracted_facts("{not json")
