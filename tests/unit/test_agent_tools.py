"""测试 check_materials 工具的实际行为与事实边界。

契约由本测试固定: agent_tools 需提供 parse_tool_arguments,
check_materials, MaterialCheckResult 与 ToolArgumentError。
解析失败或与快照不一致时一律抛 ToolArgumentError 且不执行任何规则。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vendorguard.agent_tools import (
    MaterialCheckResult,
    ToolArgumentError,
    check_materials,
    parse_tool_arguments,
)
from vendorguard.policy import PolicyDocument, RuleEvaluation, StructuredFacts, load_policy

RULES_PATH = Path("policies/rules/v1.0.0.yaml")
LICENSE_SOURCE = "BL-001@page_1"
CHECKLIST_SOURCE = "CHECKLIST-001@row_2"


@pytest.fixture(scope="module")
def policy() -> PolicyDocument:
    """加载现行规则配置, M1 只启用 VEN-001 与 VEN-002。"""

    return load_policy(RULES_PATH)


def build_facts(
    *,
    license_status: str | None = "valid",
    docs_complete: bool | None = True,
    license_source: str | None = LICENSE_SOURCE,
    checklist_source: str | None = CHECKLIST_SOURCE,
) -> StructuredFacts:
    """以正常案例为基线构造事实变体, None 字段连同其来源一并省略。"""

    data: dict[str, object] = {}
    sources: dict[str, str] = {}
    if license_status is not None:
        data["business_license_document_status"] = license_status
        if license_source is not None:
            sources["business_license_document_status"] = license_source
    if docs_complete is not None:
        data["category_required_documents_complete"] = docs_complete
        if checklist_source is not None:
            sources["category_required_documents_complete"] = checklist_source
    data["sources"] = sources
    return StructuredFacts(**data)


def as_map(result: MaterialCheckResult) -> dict[str, RuleEvaluation]:
    """按规则 ID 索引执行结果, 方便逐条断言。"""

    return {evaluation.rule_id: evaluation for evaluation in result.evaluations}


# --- 四种事实输入的规则行为 -------------------------------------------------


def test_normal_facts_both_rules_not_hit(policy: PolicyDocument) -> None:
    """完整且合法的事实两条规则均不命中, not_hit 不等于准入批准。"""

    submitted = build_facts()
    result = check_materials(submitted, policy=policy, submitted=submitted)

    evaluations = as_map(result)
    assert evaluations["VEN-001"].result == "not_hit"
    assert evaluations["VEN-002"].result == "not_hit"
    assert evaluations["VEN-001"].outcome is None
    assert evaluations["VEN-002"].outcome is None
    assert result.policy_version == policy.version
    assert result.checked_rule_ids == ["VEN-001", "VEN-002"]
    assert result.missing_fields == []
    assert result.input_sources == submitted.sources


def test_incomplete_documents_hit_request_documents(policy: PolicyDocument) -> None:
    """已知材料不齐 (false) 时 VEN-002 命中, 处置为补件。"""

    submitted = build_facts(docs_complete=False)
    result = check_materials(submitted, policy=policy, submitted=submitted)

    evaluations = as_map(result)
    assert evaluations["VEN-002"].result == "hit"
    assert evaluations["VEN-002"].outcome is not None
    assert evaluations["VEN-002"].outcome.action == "request_documents"
    assert evaluations["VEN-001"].result == "not_hit"


def test_unknown_completeness_uses_missing_input_outcome(policy: PolicyDocument) -> None:
    """完整性未知时字段键必须缺席, 走 on_missing_input 而非 not_hit。"""

    submitted = build_facts(docs_complete=None)
    result = check_materials(submitted, policy=policy, submitted=submitted)

    ven002 = as_map(result)["VEN-002"]
    assert ven002.result == "hit"
    assert ven002.outcome is not None
    assert ven002.outcome.action == "create_manual_review"
    assert result.missing_fields == ["category_required_documents_complete"]


def test_expired_license_hits_routed_outcome(policy: PolicyDocument) -> None:
    """营业执照过期时 VEN-001 经路由表命中补件处置。"""

    submitted = build_facts(license_status="expired")
    result = check_materials(submitted, policy=policy, submitted=submitted)

    ven001 = as_map(result)["VEN-001"]
    assert ven001.result == "hit"
    assert ven001.outcome is not None
    assert ven001.outcome.action == "request_documents"
    assert ven001.outcome.reason_code == "business_license_expired"


# --- 快照一致性边界 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("claimed_complete", "submitted_complete"),
    [(False, True), (True, False)],
    ids=["claimed_missing", "claimed_complete"],
)
def test_value_mismatch_rejected(
    policy: PolicyDocument, claimed_complete: bool, submitted_complete: bool
) -> None:
    """模型参数与快照的事实值不一致即补造或改写, 拒绝且不执行规则。"""

    submitted = build_facts(docs_complete=submitted_complete)
    claimed = build_facts(docs_complete=claimed_complete)
    with pytest.raises(ToolArgumentError, match="category_required_documents_complete"):
        check_materials(claimed, policy=policy, submitted=submitted)


def test_dropped_fact_rejected(policy: PolicyDocument) -> None:
    """快照已有已知事实而模型漏传该字段时按漏传拒绝。"""

    submitted = build_facts()
    claimed = build_facts(docs_complete=None)
    with pytest.raises(ToolArgumentError):
        check_materials(claimed, policy=policy, submitted=submitted)


def test_altered_source_rejected(policy: PolicyDocument) -> None:
    """事实值相同但来源被改写仍属不一致, 来源必须逐项比对。"""

    submitted = build_facts()
    claimed = build_facts(checklist_source="CHECKLIST-001@row_9")
    with pytest.raises(ToolArgumentError):
        check_materials(claimed, policy=policy, submitted=submitted)


# --- JSON 参数解析 ------------------------------------------------------------


def test_parse_valid_json_builds_structured_facts() -> None:
    """合法 JSON 参数解析为 StructuredFacts, 值与来源原样保留。"""

    payload = json.dumps(
        {
            "business_license_document_status": "valid",
            "category_required_documents_complete": True,
            "sources": {
                "business_license_document_status": LICENSE_SOURCE,
                "category_required_documents_complete": CHECKLIST_SOURCE,
            },
        }
    )
    facts = parse_tool_arguments(payload)

    assert isinstance(facts, StructuredFacts)
    assert facts.business_license_document_status == "valid"
    assert facts.category_required_documents_complete is True
    assert facts.sources == {
        "business_license_document_status": LICENSE_SOURCE,
        "category_required_documents_complete": CHECKLIST_SOURCE,
    }


def test_parse_invalid_json_raises_tool_argument_error() -> None:
    """非 JSON 文本归一为参数错误, 不向外抛 json.JSONDecodeError。"""

    with pytest.raises(ToolArgumentError):
        parse_tool_arguments("{not json")


def test_parse_fact_without_source_raises_tool_argument_error() -> None:
    """缺来源定位抛 FactsValidationError, 需归一为 ToolArgumentError。"""

    payload = json.dumps({"business_license_document_status": "valid"})
    with pytest.raises(ToolArgumentError):
        parse_tool_arguments(payload)


def test_parse_unknown_field_raises_tool_argument_error() -> None:
    """模型补造白名单外字段时 extra=forbid 拦截并归一为参数错误。"""

    payload = json.dumps({"supplier_name": "acme"})
    with pytest.raises(ToolArgumentError):
        parse_tool_arguments(payload)
