"""M3-5 报告 Schema 与引用闸门的单元测试 (纯数据, 不跑循环也不打网络).

这组测试钉住方案 7 的处理顺序:

1. 形状: 字段, 类型与多余参数由 Schema 拦下; 每条 finding 至少一个来源;
   声称"审查完整"却零 findings 无法表达; 依据不足必须在 missing_reason 说明.
2. 事实与规则结果对得上本次校验: fact_fields 必须属于本次核对通过的事实,
   材料来源必须是本次核对过的定位且与事实对应; rule_results 的规则 ID 与结果
   必须与本次 evaluations 一致.
3. 制度引用必须回指本次检索真实返回的现行版本节点, 拿 chunk_key 或旧版本节点
   冒充当引用一律拒绝.
4. 措辞兜底: 宣告准入批准/审批通过/已写入状态的表述被拦下, 带否定语的
   免责声明("不等于准入批准")必须放行.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from vendorguard.agent_report import ReviewReport, parse_report_arguments, verify_report
from vendorguard.agent_tools import ExtractedCheckResult, ToolArgumentError
from vendorguard.policy import RuleEvaluation
from vendorguard.retrieval.search import RetrievedChunk

MATERIAL_ID = "license_complete"
PAGE = "license_complete@page:1"
CURRENT_NODE = "demo_supplier_admission_policy_v2_section_2"
SUPERSEDED_NODE = "demo_supplier_admission_policy_v1_section_2"


def check_result(
    *,
    verified_sources: dict[str, str] | None = None,
    evaluations: tuple[tuple[str, str], ...] = (("VEN-001", "not_hit"), ("VEN-002", "not_hit")),
) -> ExtractedCheckResult:
    """造一份本次校验结果, 默认两条规则都未命中且两个事实都来自材料第 1 页."""

    return ExtractedCheckResult(
        policy_version="1.0.0",
        checked_rule_ids=[rule_id for rule_id, _ in evaluations],
        evaluations=[
            RuleEvaluation(rule_id=rule_id, result=result) for rule_id, result in evaluations
        ],
        missing_fields=[],
        verified_sources=verified_sources
        if verified_sources is not None
        else {
            "business_license_valid_until": PAGE,
            "category_required_documents_complete": PAGE,
        },
        derived_business_license_status="valid",
    )


def retrieved(node_key: str, edition_key: str) -> RetrievedChunk:
    """造一条本次检索返回过的节点."""

    return RetrievedChunk(
        chunk_key=f"{node_key}_main",
        node_key=node_key,
        edition_key=edition_key,
        document_title="探针制度",
        locator_path=("正常准入条件",),
        display_text="供应商同时满足下列条件的, 方可进入合格供应商清单.",
        char_start=0,
        char_end=26,
        score=0.5,
    )


def returned_nodes() -> dict[str, RetrievedChunk]:
    """本次检索真实返回过的节点: 一条现行制度节点."""

    return {CURRENT_NODE: retrieved(CURRENT_NODE, "demo_supplier_admission_policy_v2")}


def finding(**overrides: Any) -> dict:
    """一份合格 finding 的参数字典, 用例只覆盖自己关心的字段."""

    payload = {
        "summary": "营业执照声明有效期至 2027-08-31, 按参考日期判定为有效; VEN-001 未命中.",
        "fact_fields": ["business_license_valid_until"],
        "rule_results": [{"rule_id": "VEN-001", "result": "not_hit"}],
        "material_sources": [PAGE],
        "policy_citations": [],
    }
    payload.update(overrides)
    return payload


def report_payload(**overrides: Any) -> dict:
    """一份合格报告的参数字典."""

    payload: dict = {
        "material_id": MATERIAL_ID,
        "findings": [finding()],
        "insufficient_evidence": False,
        "missing_reason": "",
        "notes": "规则未命中不等于准入批准",
    }
    payload.update(overrides)
    return payload


def verified(
    payload: dict,
    *,
    checked_facts: dict[str, str] | None = None,
    result: ExtractedCheckResult | None = None,
    nodes: dict[str, RetrievedChunk] | None = None,
) -> None:
    """跑一遍闸门, 让用例只关心本步要证伪的那一条."""

    check = result if result is not None else check_result()
    verify_report(
        parse_report_arguments(json.dumps(payload, ensure_ascii=False)),
        material_id=MATERIAL_ID,
        checked_facts=check.verified_sources if checked_facts is None else checked_facts,
        check_result=check,
        returned_nodes=returned_nodes() if nodes is None else nodes,
    )


# ---------------------------------------------------------------------------
# 形状: 字段, 类型与内部矛盾
# ---------------------------------------------------------------------------


def test_documented_example_parses_and_passes_the_gate() -> None:
    """方案 7 的输入样例必须能解析并通过闸门: 字段集与引用语义以它为契约."""

    payload = {
        "material_id": MATERIAL_ID,
        "findings": [
            {
                "summary": (
                    "营业执照声明有效期至 2027-08-31, 按参考日期判定为有效; VEN-001 未命中"
                ),
                "fact_fields": ["business_license_valid_until"],
                "rule_results": [{"rule_id": "VEN-001", "result": "not_hit"}],
                "material_sources": [PAGE],
                "policy_citations": [],
            },
            {
                "summary": "正常准入条件要求营业执照未过期且关键字段可辨识",
                "fact_fields": [],
                "rule_results": [],
                "material_sources": [],
                "policy_citations": [CURRENT_NODE],
            },
        ],
        "insufficient_evidence": False,
        "missing_reason": "",
        "notes": "规则未命中不等于准入批准",
    }

    parsed = parse_report_arguments(json.dumps(payload, ensure_ascii=False))
    assert isinstance(parsed, ReviewReport)
    assert len(parsed.findings) == 2
    verified(payload)


def test_extra_field_is_rejected() -> None:
    """多余参数一律拒绝: 报告字段集是契约, 不能夹带自定义字段."""

    with pytest.raises(ToolArgumentError, match="Schema"):
        parse_report_arguments(json.dumps({**report_payload(), "approved": True}))


def test_finding_without_any_source_is_rejected() -> None:
    """每条 finding 至少要有一个来源."""

    with pytest.raises(ToolArgumentError, match="至少"):
        parse_report_arguments(
            json.dumps(
                report_payload(
                    findings=[finding(material_sources=[], policy_citations=[])],
                )
            )
        )


def test_claiming_complete_without_findings_is_rejected() -> None:
    """零引用却声称审查完整: Schema 层就无法表达."""

    with pytest.raises(ToolArgumentError, match="零 findings"):
        parse_report_arguments(json.dumps(report_payload(findings=[])))


def test_insufficient_evidence_requires_a_reason() -> None:
    """依据不足必须说清缺哪类依据."""

    with pytest.raises(ToolArgumentError, match="missing_reason"):
        parse_report_arguments(
            json.dumps(report_payload(findings=[], insufficient_evidence=True, missing_reason=""))
        )


def test_insufficient_report_with_reason_passes() -> None:
    """明确缺依据的空 findings 报告是合法结论."""

    payload = report_payload(
        findings=[],
        insufficient_evidence=True,
        missing_reason="材料里没有有效期截止日, 无法判断营业执照是否有效",
    )

    parsed = parse_report_arguments(json.dumps(payload, ensure_ascii=False))
    assert parsed.insufficient_evidence is True
    verified(payload)


# ---------------------------------------------------------------------------
# 事实与规则结果必须对得上本次校验
# ---------------------------------------------------------------------------


def test_fabricated_fact_field_is_rejected() -> None:
    """捏造事实字段: 本次没核对过的事实不能进报告."""

    payload = report_payload(findings=[finding(fact_fields=["supplier_credit_rating"])])

    with pytest.raises(ToolArgumentError, match="不是本次核对通过的事实"):
        verified(payload)


def test_unknown_material_source_is_rejected() -> None:
    """材料来源必须是本次核对过的定位, 不能编页码."""

    payload = report_payload(findings=[finding(material_sources=["license_complete@page:9"])])

    with pytest.raises(ToolArgumentError, match="不属于本次已核对"):
        verified(payload)


def test_material_source_must_match_the_verified_locator() -> None:
    """声明事实却引用别的来源: 事实与来源对不上同样拒绝."""

    check = check_result(
        verified_sources={
            "business_license_valid_until": "license_complete@page:1",
            "category_required_documents_complete": "license_complete@page:2",
        }
    )
    payload = report_payload(
        findings=[
            finding(
                fact_fields=["category_required_documents_complete"],
                material_sources=["license_complete@page:1"],
            )
        ]
    )

    with pytest.raises(ToolArgumentError, match="本次核对的来源"):
        verified(payload, result=check)


def test_unknown_rule_is_rejected() -> None:
    """报告不能引用本次没执行过的规则."""

    payload = report_payload(
        findings=[finding(rule_results=[{"rule_id": "VEN-005", "result": "hit"}])]
    )

    with pytest.raises(ToolArgumentError, match="不在本次执行范围"):
        verified(payload)


def test_wrong_rule_result_is_rejected() -> None:
    """把未命中写成命中: 规则结果必须与本次 evaluation 一致."""

    payload = report_payload(
        findings=[finding(rule_results=[{"rule_id": "VEN-001", "result": "hit"}])]
    )

    with pytest.raises(ToolArgumentError, match="实际结果是 not_hit"):
        verified(payload)


# ---------------------------------------------------------------------------
# 制度引用必须回指本次检索返回的现行节点
# ---------------------------------------------------------------------------


def test_fabricated_citation_is_rejected() -> None:
    """引用没检索过的节点: 编不出依据."""

    payload = report_payload(findings=[finding(policy_citations=[SUPERSEDED_NODE])])

    with pytest.raises(ToolArgumentError, match="不是本次检索返回过的节点"):
        verified(payload)


def test_superseded_edition_citation_is_rejected() -> None:
    """引用被替代的版本: 即使节点在场也不能当依据."""

    nodes = {
        SUPERSEDED_NODE: retrieved(SUPERSEDED_NODE, "demo_supplier_admission_policy_v1"),
    }
    payload = report_payload(findings=[finding(policy_citations=[SUPERSEDED_NODE])])

    with pytest.raises(ToolArgumentError, match="非现行版本"):
        verified(payload, nodes=nodes)


def test_returned_current_node_is_accepted() -> None:
    """本次检索真实返回的现行节点可以引用."""

    verified(report_payload(findings=[finding(policy_citations=[CURRENT_NODE])]))


# ---------------------------------------------------------------------------
# 措辞兜底
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "该供应商准入批准已完成.",
        "营业执照有效, 供应商已批准进入合格清单.",
        "两条规则均未命中, 准入审批通过.",
        "材料齐全, 建议通过准入.",
        "系统已写入案件状态为已批准.",
    ],
)
def test_approval_wording_is_rejected(summary: str) -> None:
    """宣告准入批准或声称写入状态一律拒绝."""

    payload = report_payload(findings=[finding(summary=summary)])

    with pytest.raises(ToolArgumentError, match="措辞越权"):
        verified(payload)


@pytest.mark.parametrize(
    "text",
    [
        "规则未命中不等于准入批准",
        "本次运行没有写入案件状态, 也不代表通过准入",
        "规则未命中不是准入批准, 仍需人工决定",
        # 真实模型在 M3-5 首次端到端运行里写的免责声明, 曾被误判成宣告
        "规则未命中(not_hit)表示未发现不符合项, 但不等同于最终准入批准.",
        "材料齐全, 但不表示准入审批通过.",
    ],
)
def test_negated_wording_is_allowed(text: str) -> None:
    """带否定语的免责声明必须放行, 否则会把合规表述误伤."""

    verified(report_payload(notes=text))


def test_material_id_must_match_the_run() -> None:
    """报告声明的材料必须是本次审查的那一份."""

    payload = report_payload(material_id="another_supplier")

    with pytest.raises(ToolArgumentError, match="不是本次审查的材料"):
        verified(payload)
