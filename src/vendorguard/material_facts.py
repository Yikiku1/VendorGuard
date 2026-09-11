"""从材料提取层事实派生规则输入, 并对来源文本做字面核对.

与 M1 的关键区别: M1 的模型只回显程序持有的事实, 所以"模型不能篡改"
靠快照对账解决; 这里的事实第一次来自模型读数, 程序侧没有真值可比, 边界
改为"**每个事实值必须在其声明的来源文本里字面命中**"。

两点设计约束:

- 日期状态由程序按固定参考日期派生, 模型不产出 `valid` / `expired` 枚举,
  因此它没有"自己声明结论"的权力。
- 布尔类判断(清单是否齐全)只能核对所引用的页码真实存在, 无法验证判断
  本身正确; 这条不对称必须如实保留, 不得宣称全部事实已程序验证。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, field_validator

from .materials import SourceTexts
from .policy import (
    FactsValidationError,
    PolicyDocument,
    RuleEvaluation,
    StructuredFacts,
    evaluate_rule,
)

_FACT_FIELDS = (
    "business_license_document_status",
    "category_required_documents_complete",
)


class ExtractionError(ValueError):
    """模型提交的提取事实非法或来源核对失败时抛出的错误。"""


class ExtractedFacts(BaseModel):
    """模型从材料读数提交的原始事实, 尚未换算为规则输入。

    字段与规则输入层刻意不同: 模型只提交"声明有效期"这个可核对的事实,
    状态枚举由程序派生。`sources` 的键必须是已提交的事实字段, 与既有
    `StructuredFacts` 的来源契约一致。
    """

    model_config = ConfigDict(extra="forbid")

    business_license_declared_valid_until: date | None = None
    category_required_documents_complete: bool | None = None
    sources: dict[str, str] = {}

    @field_validator("sources")
    @classmethod
    def validate_source_keys(cls, value: dict[str, str]) -> dict[str, str]:
        """来源只能引用本次确实提交的字段。"""

        for key in value:
            if key not in (
                "business_license_declared_valid_until",
                "category_required_documents_complete",
            ):
                raise ValueError(f"来源定位引用了未知事实: {key}")
        return value


def derive_business_license_status(
    *,
    declared_valid_until: date | None,
    reference_date: date,
) -> str | None:
    """由声明有效期与参考日期派生营业执照材料状态。

    到期日当天仍视为有效(与规则配置的 date_boundary 约定一致); 缺日期
    返回 None, 进入既有的 missing_fields 通道, 不会被算成任何一种状态,
    也不会被误判为规则通过。不产出 `inconsistent` / `unreadable`: 前者
    需要申请表核对, 后者需要可读性判定, 都不在本期范围。
    """

    if declared_valid_until is None:
        return None
    return "valid" if declared_valid_until >= reference_date else "expired"


def _locator_for(facts: ExtractedFacts, field: str) -> str:
    locator = facts.sources.get(field)
    if locator is None or not locator.strip():
        raise ExtractionError(f"事实 {field} 缺少来源定位")
    return locator


def verify_literal_sources(facts: ExtractedFacts, *, sources: SourceTexts) -> list[str]:
    """逐字段核对事实值是否真的出现在所声明的来源里。

    核对通过返回本次被核对的事实字段名列表, 供运行记录留痕。日期做数字组
    比对, 布尔判断只核对来源页码存在——后者不能机械化验证, 返回值不区分
    两种核对强度, 该限制由 `verified_as` 之外的上层文档声明。
    """

    verified: list[str] = []

    if facts.business_license_declared_valid_until is not None:
        locator = _locator_for(facts, "business_license_declared_valid_until")
        if not sources.contains_date(locator, facts.business_license_declared_valid_until):
            raise ExtractionError(
                "声明有效期 "
                f"{facts.business_license_declared_valid_until.isoformat()} "
                f"未出现在所声明的来源 {locator} 中, 拒绝采信模型读数"
            )
        verified.append("business_license_declared_valid_until")

    if facts.category_required_documents_complete is not None:
        locator = _locator_for(facts, "category_required_documents_complete")
        if not sources.contains_flag(locator):
            raise ExtractionError(f"清单判断的来源 {locator} 不存在或为空白页, 拒绝采信")
        verified.append("category_required_documents_complete")

    return verified


class MaterialCheckResult(BaseModel):
    """材料校验的聚合输出, 规则语义复用 RuleEvaluation。"""

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    reference_date: date
    checked_rule_ids: list[str]
    evaluations: list[RuleEvaluation]
    missing_fields: list[str]
    input_sources: dict[str, str]
    verified_fact_fields: list[str]
    derived_business_license_status: str | None


def check_extracted_facts(
    facts: ExtractedFacts,
    *,
    policy: PolicyDocument,
    sources: SourceTexts,
    reference_date: date,
) -> MaterialCheckResult:
    """核对来源后, 把提取事实换算为规则输入并执行启用规则。

    policy / sources / reference_date 都是程序持有的关键字参数, 不由模型
    提供。核对失败时抛出 ExtractionError, 任何规则都不会被执行——这与
    M1"对账失败不执行规则"的边界一致。
    """

    verified = verify_literal_sources(facts, sources=sources)

    status = derive_business_license_status(
        declared_valid_until=facts.business_license_declared_valid_until,
        reference_date=reference_date,
    )

    # 组装规则输入层: 字段名与白名单不变, 值来自程序派生而非模型声明。
    data: dict[str, object] = {
        "business_license_document_status": status,
        "category_required_documents_complete": facts.category_required_documents_complete,
    }
    # 只有模型确实提交过的字段才带来源, 否则 StructuredFacts 的来源校验会拒绝。
    locators = {
        "business_license_document_status": facts.sources.get(
            "business_license_declared_valid_until"
        ),
        "category_required_documents_complete": facts.sources.get(
            "category_required_documents_complete"
        ),
    }
    data["sources"] = {
        field: locator
        for field, locator in locators.items()
        if locator is not None and data.get(field) is not None
    }

    try:
        structured = StructuredFacts(**data)
    except FactsValidationError as exc:
        raise ExtractionError(f"换算后的规则输入未通过校验: {exc}") from exc

    values = structured.model_dump(exclude={"sources"}, exclude_none=True)
    enabled = set(policy.implementation_scope.enabled_rule_ids)
    checked_rule_ids: list[str] = []
    evaluations: list[RuleEvaluation] = []
    for rule in policy.rules:
        if rule.id not in enabled:
            continue
        checked_rule_ids.append(rule.id)
        evaluations.append(evaluate_rule(rule, scope="supplier_admission", facts=values))

    return MaterialCheckResult(
        policy_version=policy.version,
        reference_date=reference_date,
        checked_rule_ids=checked_rule_ids,
        evaluations=evaluations,
        missing_fields=[field for field in _FACT_FIELDS if field not in values],
        input_sources=dict(structured.sources),
        verified_fact_fields=verified,
        derived_business_license_status=status,
    )


__all__ = [
    "ExtractedFacts",
    "ExtractionError",
    "MaterialCheckResult",
    "check_extracted_facts",
    "derive_business_license_status",
    "verify_literal_sources",
]
