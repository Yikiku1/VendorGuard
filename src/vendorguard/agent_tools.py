"""把确定性规则包装成 Agent 可调用的 check_materials 工具。

本模块只做四件事: 解析并校验模型参数, 与运行器持有的真实提交快照
对账, 复用 evaluate_rule 执行启用规则, 聚合结果。不推进任何案件
状态, 不访问数据库。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from .policy import (
    FactsValidationError,
    PolicyDocument,
    RuleEvaluation,
    StructuredFacts,
    evaluate_rule,
)

# 与 facts.py 的白名单一致, 顺序用于确定性的缺字段报告。
_FACT_FIELDS = (
    "business_license_document_status",
    "category_required_documents_complete",
)


class ToolArgumentError(ValueError):
    """模型提交的工具参数非法或与快照不一致时抛出的错误。

    D 阶段运行器捕获后转成结构化错误回传给模型, 因此消息必须
    指明具体字段, 让模型有机会按错误纠正。
    """


class MaterialCheckResult(BaseModel):
    """一次材料校验的聚合输出, 规则语义直接复用 RuleEvaluation。"""

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    checked_rule_ids: list[str]
    evaluations: list[RuleEvaluation]
    missing_fields: list[str]
    input_sources: dict[str, str]


def parse_tool_arguments(raw_json: str) -> StructuredFacts:
    """把模型提交的 JSON 参数解析为已校验的结构化事实。

    坏 JSON 与 FactsValidationError 都归一为 ToolArgumentError,
    调用方只需处理一种错误类型。
    """

    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:  # JSONDecodeError 是 ValueError 子类
        raise ToolArgumentError(f"工具参数不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ToolArgumentError("工具参数必须是 JSON 对象")

    try:
        return StructuredFacts(**raw)
    except FactsValidationError as exc:
        raise ToolArgumentError(f"工具参数未通过事实 Schema 校验: {exc}") from exc


def _verify_against_snapshot(facts: StructuredFacts, submitted: StructuredFacts) -> None:
    """逐项比对模型参数与真实提交快照的事实值和来源。

    补造, 改写和漏传都会体现为某个字段的值或来源不相等, 必须
    在任何规则执行之前拒绝。
    """

    for field in _FACT_FIELDS:
        if getattr(facts, field) != getattr(submitted, field):
            raise ToolArgumentError(
                f"事实 {field} 与本次提交快照不一致, 模型不得修改用户提交的事实"
            )
        if facts.sources.get(field) != submitted.sources.get(field):
            raise ToolArgumentError(f"事实 {field} 的来源与本次提交快照不一致")


def check_materials(
    facts: StructuredFacts,
    *,
    policy: PolicyDocument,
    submitted: StructuredFacts,
) -> MaterialCheckResult:
    """对账通过后, 用启用规则检查材料并聚合结果。

    policy 与 submitted 都是程序持有的关键字参数, 不由模型提供;
    facts 是模型提交的参数, 必须先通过对账。
    """

    _verify_against_snapshot(facts, submitted)

    # 关键 1: exclude_none=True。执行器靠键是否存在识别缺输入,
    # None 留在字典里会把 "未知" 错报成 "not_hit"。
    values: dict[str, Any] = facts.model_dump(exclude={"sources"}, exclude_none=True)

    # 关键 2: 只遍历启用规则, 以 supplier_admission 执行。
    enabled = set(policy.implementation_scope.enabled_rule_ids)
    checked_rule_ids: list[str] = []
    evaluations: list[RuleEvaluation] = []
    for rule in policy.rules:
        if rule.id not in enabled:
            continue
        checked_rule_ids.append(rule.id)
        evaluations.append(evaluate_rule(rule, scope="supplier_admission", facts=values))

    # 关键 3: 缺字段用 None 判定而不是查 values, 保持白名单顺序。
    return MaterialCheckResult(
        policy_version=policy.version,
        checked_rule_ids=checked_rule_ids,
        evaluations=evaluations,
        missing_fields=[field for field in _FACT_FIELDS if getattr(facts, field) is None],
        input_sources=dict(facts.sources),
    )


__all__ = [
    "MaterialCheckResult",
    "ToolArgumentError",
    "check_materials",
    "parse_tool_arguments",
]
