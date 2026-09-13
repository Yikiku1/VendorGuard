"""把确定性规则包装成 Agent 可调用的材料检查工具。

本模块只做四件事: 解析并校验模型参数, 与来源对账, 复用 evaluate_rule
执行启用规则, 聚合结果。不推进任何案件状态, 不访问数据库。

本模块现有两条入口: M2 的提取事实管线 (parse_extracted_facts /
check_extracted_facts) 核对模型从材料里读出的原始事实; M1 的快照对账
(parse_tool_arguments / check_materials) 仍由 agent.py 使用, 等 M2-3 把
循环切到提取事实管线后删除。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from .materials import MaterialSources, parse_declared_date
from .policy import (
    FactsValidationError,
    PolicyDocument,
    RuleEvaluation,
    StructuredFacts,
    certificate_remaining_days,
    evaluate_rule,
)

# 规则消费的事实白名单, 顺序用于确定性的缺字段报告。
_FACT_FIELDS = (
    "business_license_document_status",
    "category_required_documents_complete",
)

# 模型能从材料里读出的原始事实白名单, 顺序同样用于确定性报错。
_EXTRACTED_FACT_FIELDS = (
    "business_license_valid_until",
    "category_required_documents_complete",
)

# 程序按参考日期派生的执照状态: 模型只能提交日期, 不能提交这个结论。
DerivedLicenseStatus = Literal["valid", "expired"]


class ToolArgumentError(ValueError):
    """模型提交的工具参数非法或与来源不一致时抛出的错误。

    运行器捕获后转成结构化错误回传给模型, 因此消息必须指明具体字段,
    让模型有机会按错误纠正。
    """


class MaterialCheckResult(BaseModel):
    """一次材料校验的聚合输出, 规则语义直接复用 RuleEvaluation。"""

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    checked_rule_ids: list[str]
    evaluations: list[RuleEvaluation]
    missing_fields: list[str]
    input_sources: dict[str, str]


class ExtractedMaterialFacts(BaseModel):
    """模型从材料里读出的原始事实与来源声明。

    这里只允许材料上真能读到的东西: 声明有效期的截止日、清单是否齐全,
    以及每条事实的来源定位。"执照是否有效"是程序按参考日期派生的结论,
    模型不许直接提交 (M2 方案 3.2), 所以这个 Schema 里没有状态字段。
    """

    model_config = ConfigDict(extra="forbid")

    business_license_valid_until: date | None = None
    category_required_documents_complete: StrictBool | None = None
    sources: dict[str, str] = Field(default_factory=dict)

    @field_validator("business_license_valid_until", mode="before")
    @classmethod
    def _normalize_declared_date(cls, value: object) -> object:
        """只接受固定格式的日期字符串, 归一为 date; 其余写法一律拒绝。"""

        if value is None or isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError(f"日期必须是字符串, 收到 {type(value).__name__}")
        return parse_declared_date(value)

    @model_validator(mode="after")
    def _require_sources(self) -> ExtractedMaterialFacts:
        """每个已声明的事实都必须带来源, 来源也不能引用未知事实。"""

        for field in _EXTRACTED_FACT_FIELDS:
            if getattr(self, field) is None:
                continue
            if not self.sources.get(field):
                raise ValueError(f"事实 {field} 缺少来源定位")
        for key in self.sources:
            if key not in _EXTRACTED_FACT_FIELDS:
                raise ValueError(f"来源定位引用了未知事实: {key}")
        return self


class ExtractedCheckResult(BaseModel):
    """提取事实管线的聚合输出。

    verified_sources 记录"哪条声明的事实由哪个来源核实", 键是模型声明的事实名;
    派生状态的来源就是 business_license_valid_until 的那一条, 因为结论正是从
    那一页的日期推出来的。missing_fields 用模型能读懂的事实名报告, 模型据此
    通过 ask_user 向用户追问, 而不是去猜派生字段。
    """

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    checked_rule_ids: list[str]
    evaluations: list[RuleEvaluation]
    missing_fields: list[str]
    verified_sources: dict[str, str]
    derived_business_license_status: DerivedLicenseStatus | None


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


def parse_extracted_facts(raw_json: str) -> ExtractedMaterialFacts:
    """把模型提交的 JSON 参数解析为已校验的提取事实。

    坏 JSON、日期写法不受支持、缺来源或多余字段都在这一步被拦下, 并统一
    归一为 ToolArgumentError, 调用方只需处理一种错误类型。
    """

    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:  # JSONDecodeError 是 ValueError 子类
        raise ToolArgumentError(f"工具参数不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ToolArgumentError("工具参数必须是 JSON 对象")

    try:
        return ExtractedMaterialFacts(**raw)
    except ValidationError as exc:
        raise ToolArgumentError(f"工具参数未通过提取事实 Schema 校验: {exc}") from exc


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


def _evaluate_enabled_rules(
    policy: PolicyDocument, values: dict[str, Any]
) -> tuple[list[str], list[RuleEvaluation]]:
    """按启用顺序执行本期规则, 返回规则 ID 与执行结果。

    两条入口共用这一份"哪些规则进入本期"的判定, 避免出现第二套规则清单:
    只遍历 implementation_scope 里的启用规则, 统一以 supplier_admission 执行。
    """

    enabled = set(policy.implementation_scope.enabled_rule_ids)
    checked_rule_ids: list[str] = []
    evaluations: list[RuleEvaluation] = []
    for rule in policy.rules:
        if rule.id not in enabled:
            continue
        checked_rule_ids.append(rule.id)
        evaluations.append(evaluate_rule(rule, scope="supplier_admission", facts=values))
    return checked_rule_ids, evaluations


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
    checked_rule_ids, evaluations = _evaluate_enabled_rules(policy, values)

    # 关键 3: 缺字段用 None 判定而不是查 values, 保持白名单顺序。
    return MaterialCheckResult(
        policy_version=policy.version,
        checked_rule_ids=checked_rule_ids,
        evaluations=evaluations,
        missing_fields=[field for field in _FACT_FIELDS if getattr(facts, field) is None],
        input_sources=dict(facts.sources),
    )


def check_extracted_facts(
    facts: ExtractedMaterialFacts,
    *,
    sources: MaterialSources,
    policy: PolicyDocument,
    reference_date: date,
) -> ExtractedCheckResult:
    """核对来源、派生执照状态, 再用启用规则检查材料。

    facts 是模型提交的参数; sources、policy 与 reference_date 都由程序持有,
    模型拿不到也改不了。处理顺序固定, 任一步失败都抛 ToolArgumentError 且
    不执行后续步骤 (M2 方案第 5 节), 因此模型编的日期、伪造的页码和反着报的
    清单状态都不可能走到规则执行。
    """

    # 第一步: 每个来源都必须能在本次材料或已登记的用户补充里取回原文。
    # 伪造页码、未登记的材料 ID、未登记的补充轮次都在这一步失败。
    for field in _EXTRACTED_FACT_FIELDS:
        locator = facts.sources.get(field)
        if locator is not None and sources.text_for(locator) is None:
            raise ToolArgumentError(
                f"事实 {field} 的来源 {locator} 不属于本次材料或已登记的用户补充"
            )

    # 第二步: 声明的值必须在来源原文里字面命中。改日期、反着报清单都会失败。
    valid_until = facts.business_license_valid_until
    if valid_until is not None:
        date_locator = facts.sources["business_license_valid_until"]
        if not sources.contains_date(date_locator, valid_until):
            raise ToolArgumentError(
                f"事实 business_license_valid_until 声明的日期没有出现在来源 {date_locator} 里"
            )
    docs_complete = facts.category_required_documents_complete
    if docs_complete is not None:
        checklist_locator = facts.sources["category_required_documents_complete"]
        if not sources.contains_checklist_complete(checklist_locator, complete=docs_complete):
            raise ToolArgumentError(
                "事实 category_required_documents_complete 声明的清单状态"
                f"没有出现在来源 {checklist_locator} 里"
            )

    # 第三步: 执照状态由程序按参考日期派生。模型提交的是日期, 是否有效由
    # 参考日期决定 (到期日当天仍有效); 派生沿用同一条来源, 因为结论就是从
    # 那一页的日期推出来的。
    declared: dict[str, Any] = {}
    rule_sources: dict[str, str] = {}
    derived_status: DerivedLicenseStatus | None = None
    if valid_until is not None:
        remaining_days = certificate_remaining_days(
            valid_until=valid_until, reference_date=reference_date
        )
        derived_status = "expired" if remaining_days < 0 else "valid"
        declared["business_license_document_status"] = derived_status
        rule_sources["business_license_document_status"] = facts.sources[
            "business_license_valid_until"
        ]
    if docs_complete is not None:
        declared["category_required_documents_complete"] = docs_complete
        rule_sources["category_required_documents_complete"] = facts.sources[
            "category_required_documents_complete"
        ]

    # 第四步: 组装规则输入。形状与来源合法性由既有 Schema 兜住, 校验失败仍
    # 归一为 ToolArgumentError, 工具层对外只有一种错误类型。
    try:
        structured = StructuredFacts(**declared, sources=rule_sources)
    except FactsValidationError as exc:
        raise ToolArgumentError(f"派生事实组装失败: {exc}") from exc

    # 关键: exclude_none=True, 否则缺输入会被执行器当成已知值报成 not_hit。
    values: dict[str, Any] = structured.model_dump(exclude={"sources"}, exclude_none=True)

    # 第五步: 复用既有规则执行器。
    checked_rule_ids, evaluations = _evaluate_enabled_rules(policy, values)

    return ExtractedCheckResult(
        policy_version=policy.version,
        checked_rule_ids=checked_rule_ids,
        evaluations=evaluations,
        missing_fields=[field for field in _EXTRACTED_FACT_FIELDS if getattr(facts, field) is None],
        verified_sources=dict(facts.sources),
        derived_business_license_status=derived_status,
    )


__all__ = [
    "DerivedLicenseStatus",
    "ExtractedCheckResult",
    "ExtractedMaterialFacts",
    "MaterialCheckResult",
    "ToolArgumentError",
    "check_extracted_facts",
    "check_materials",
    "parse_extracted_facts",
    "parse_tool_arguments",
]
