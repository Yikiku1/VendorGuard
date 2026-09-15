"""带来源的审查报告: 报告 Schema, 引用闸门与越权措辞兜底.

设计要点 (对应 M3 方案 3.1, 3.3, 7 与 9 的 M3-5):

- 报告只能复述本次真实发生过的工具结果: `fact_fields` 与 `material_sources` 必须
  对得上本次成功 `check_materials` 的已核对事实, `rule_results` 必须对得上本次
  `evaluations`, `policy_citations` 必须是本次 `search_policy` 真实返回过的节点.
  程序核对的只是这些结构字段, 不宣称能核对任意自由文本摘要——自然语言是否受原文
  支持仍由固定案例人工核对.
- 引用单位是 `node_key`: `chunk_key` 是召回编号, 拿它当引用一律拒绝; 版本不在现行
  清单里的节点同样拒绝.
- 措辞检查是**黑名单兜底**: 拦"已批准/审批通过/已写入"这类宣告, 但带否定语的表述
  ("不等于准入批准")必须放行, 否则会误伤免责声明. 它不是完备的语义检查.
- 依据不足是合法结论: `insufficient_evidence=true` 时允许空 findings, 但必须在
  `missing_reason` 说明缺什么; 反过来, 声称审查完整却零 findings 会被拒绝.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vendorguard.agent_tools import ExtractedCheckResult, ToolArgumentError
from vendorguard.policy.schema import RuleResult
from vendorguard.retrieval.chunk_list import CURRENT_EDITION_KEYS
from vendorguard.retrieval.search import RetrievedChunk

# 越权措辞黑名单: 都是"宣告已完成审批"或"声称系统已写入"的说法.
# 带否定语前缀的表述放行, 例如"规则未命中不等于准入批准".
_FORBIDDEN_WORDING = (
    "准入批准",
    "通过准入",
    "准予准入",
    "已准入",
    "审批通过",
    "批准通过",
    "已批准",
    "已写入",
    "已更新",
    "已标记",
    "系统已",
)
# 否定语只认明确的否定词, 不能收"未"这种单字: "两条规则均未命中, 准入审批通过"
# 里的"未"会落在宣告语的窗口内, 把真正的越权宣告放行过去.
# "不等同" 这类免责声明必须放行——真实模型就写过"不等同于最终准入批准".
_NEGATION_WORDS = (
    "不等于",
    "不等同",
    "不是",
    "并非",
    "不代表",
    "不表示",
    "不意味着",
    "不能说明",
    "不得",
    "不能",
    "没有",
)
_NEGATION_WINDOW = 10


class RuleResultClaim(BaseModel):
    """报告里对一条规则结果的复述, 必须与本次真实 evaluation 逐字对应."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1)
    result: RuleResult


class Finding(BaseModel):
    """一条审查发现: 结论文本加上它引用的材料事实与制度节点."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    fact_fields: tuple[str, ...] = ()
    rule_results: tuple[RuleResultClaim, ...] = ()
    material_sources: tuple[str, ...] = ()
    policy_citations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_has_a_source(self) -> Finding:
        """每条发现至少要有一个来源: 材料定位或制度节点 (方案 7 第 2 步)."""

        if not self.material_sources and not self.policy_citations:
            raise ValueError("每条 finding 至少要有 material_sources 或 policy_citations 之一")
        return self


class ReviewReport(BaseModel):
    """一次审查的结构化报告, 是本次运行唯一的结论出口."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    material_id: str = Field(min_length=1)
    findings: tuple[Finding, ...] = ()
    insufficient_evidence: bool = False
    missing_reason: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def validate_evidence_claim(self) -> ReviewReport:
        """依据不足与"审查完整"不能同时成立.

        两个状态互斥: 要么给出发现并声称已核对, 要么明确写清缺少哪类依据.
        这样"零引用却声称完整"在 Schema 层就无法表达.
        """

        if self.insufficient_evidence:
            if not self.missing_reason.strip():
                raise ValueError(
                    "insufficient_evidence 为 true 时必须在 missing_reason 说明缺哪类依据"
                )
            return self

        if not self.findings:
            raise ValueError(
                "零 findings 的报告不能声称审查完整: 要么给出发现, 要么标记 insufficient_evidence"
            )
        return self


def parse_report_arguments(raw_json: str) -> ReviewReport:
    """把模型提交的报告参数解析为已校验的 ReviewReport.

    只做形状校验 (字段, 类型, 多余参数, 内部矛盾); 与本次真实结果是否一致由
    verify_report 负责, 两者失败都归一为 ToolArgumentError 回到纠正通道.
    """

    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ToolArgumentError(f"报告参数不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ToolArgumentError("报告参数必须是 JSON 对象")

    try:
        return ReviewReport(**raw)
    except ValidationError as exc:
        raise ToolArgumentError(f"报告未通过 Schema 校验: {exc}") from exc


def verify_report(
    report: ReviewReport,
    *,
    material_id: str,
    checked_facts: Mapping[str, str],
    check_result: ExtractedCheckResult,
    returned_nodes: Mapping[str, RetrievedChunk],
) -> None:
    """核对报告与本次真实结果是否一致, 不一致抛 ToolArgumentError.

    checked_facts 是本次核对通过的事实名到来源定位的映射 (即
    check_result.verified_sources). 单独作为参数传入是留一道接缝: 将来若事实来自
    别的管线, 闸门不必依赖 check_result 的内部结构.
    """

    if report.material_id != material_id:
        raise ToolArgumentError(
            f"报告的 material_id {report.material_id} 不是本次审查的材料 {material_id}"
        )

    _verify_wording(report)

    known_rules = {item.rule_id: item.result for item in check_result.evaluations}

    for index, finding in enumerate(report.findings, start=1):
        where = f"第 {index} 条 finding"
        _verify_fact_fields(finding, checked_facts=checked_facts, where=where)
        _verify_material_sources(finding, checked_facts=checked_facts, where=where)
        _verify_rule_results(finding, known_rules=known_rules, where=where)
        _verify_citations(finding, returned_nodes=returned_nodes, where=where)


def _verify_wording(report: ReviewReport) -> None:
    """扫描报告里的自由文本, 拦下宣告审批完成或声称写入状态的说法."""

    texts = [finding.summary for finding in report.findings]
    texts.extend([report.notes, report.missing_reason])
    for text in texts:
        for phrase in _FORBIDDEN_WORDING:
            start = text.find(phrase)
            while start >= 0:
                prefix = text[max(0, start - _NEGATION_WINDOW) : start]
                if not any(word in prefix for word in _NEGATION_WORDS):
                    raise ToolArgumentError(
                        f"报告措辞越权: 出现 {phrase!r} 这类宣告; "
                        "只能描述本次检查结果与规则给出的建议动作(如补件或人工审批), "
                        "不得写成准入已经批准或通过, 也不得声称系统已写入或更新状态"
                    )
                start = text.find(phrase, start + 1)


def _verify_fact_fields(
    finding: Finding,
    *,
    checked_facts: Mapping[str, str],
    where: str,
) -> None:
    """每条事实字段都必须是本次核对通过的事实."""

    for field in finding.fact_fields:
        if field not in checked_facts:
            raise ToolArgumentError(
                f"{where} 声明的事实 {field} 不是本次核对通过的事实; "
                f"本次可用的事实是 {sorted(checked_facts)}"
            )


def _verify_material_sources(
    finding: Finding,
    *,
    checked_facts: Mapping[str, str],
    where: str,
) -> None:
    """材料来源必须是本次核对过的定位, 且与声明的事实对应同一个定位."""

    verified_locators = set(checked_facts.values())
    for locator in finding.material_sources:
        if locator not in verified_locators:
            raise ToolArgumentError(
                f"{where} 的材料来源 {locator} 不属于本次已核对的来源; "
                f"本次可用来源是 {sorted(verified_locators)}"
            )

    for field in finding.fact_fields:
        expected = checked_facts.get(field)
        if expected is not None and expected not in finding.material_sources:
            raise ToolArgumentError(
                f"{where} 声明事实 {field} 却把来源写成 {list(finding.material_sources)}; "
                f"该事实本次核对的来源是 {expected}"
            )


def _verify_rule_results(
    finding: Finding,
    *,
    known_rules: Mapping[str, str],
    where: str,
) -> None:
    """规则 ID 必须是本次执行过的规则, 结果必须与本次 evaluation 一致."""

    for claim in finding.rule_results:
        actual = known_rules.get(claim.rule_id)
        if actual is None:
            raise ToolArgumentError(
                f"{where} 声明的规则 {claim.rule_id} 不在本次执行范围; "
                f"本次执行的是 {sorted(known_rules)}"
            )
        if claim.result != actual:
            raise ToolArgumentError(
                f"{where} 把规则 {claim.rule_id} 的结果写成 {claim.result}, 本次实际结果是 {actual}"
            )


def _verify_citations(
    finding: Finding,
    *,
    returned_nodes: Mapping[str, RetrievedChunk],
    where: str,
) -> None:
    """制度引用必须是本次检索真实返回过的现行版本节点."""

    for node_key in finding.policy_citations:
        node = returned_nodes.get(node_key)
        if node is None:
            raise ToolArgumentError(
                f"{where} 引用的节点 {node_key} 不是本次检索返回过的节点; "
                f"本次可引用的节点是 {sorted(returned_nodes)}"
            )
        if node.edition_key not in CURRENT_EDITION_KEYS:
            raise ToolArgumentError(
                f"{where} 引用的节点 {node_key} 属于非现行版本 {node.edition_key}, 不能作为依据"
            )


__all__ = [
    "Finding",
    "ReviewReport",
    "RuleResultClaim",
    "parse_report_arguments",
    "verify_report",
]
