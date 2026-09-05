"""执行版本化策略中的确定性规则。"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from .schema import (
    PolicyScope,
    RuleCondition,
    RuleDefinition,
    RuleOutcome,
    RuleResult,
)


class RuleEvaluation(BaseModel):
    """描述一条确定性规则的单次执行结果。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    result: RuleResult
    outcome: RuleOutcome | None = None


def evaluate_rule(
    rule: RuleDefinition,
    *,
    scope: PolicyScope,
    facts: Mapping[str, object],
) -> RuleEvaluation:
    """使用结构化事实执行一条规则。"""

    if scope not in rule.scopes:
        return RuleEvaluation(
            rule_id=rule.id,
            result="not_applicable",
        )

    field = rule.condition.field
    if field not in facts:
        return RuleEvaluation(
            rule_id=rule.id,
            result="hit",
            outcome=rule.on_missing_input,
        )

    value = facts[field]
    if not _condition_matches(value, rule.condition):
        return RuleEvaluation(
            rule_id=rule.id,
            result="not_hit",
        )

    return RuleEvaluation(
        rule_id=rule.id,
        result="hit",
        outcome=_resolve_hit_outcome(rule.hit_outcome, value),
    )


def _condition_matches(value: object, condition: RuleCondition) -> bool:
    """按条件运算符判断事实是否命中! 未支持的运算符显式抛错。"""

    if condition.operator == "in":
        return value in condition.value
    if condition.operator == "equals":
        # 布尔规则要求事实值类型也必须是 bool, 避免 Python 里 0 == False 的误命中。
        if isinstance(condition.value, bool):
            return isinstance(value, bool) and value is condition.value
        return bool(value == condition.value)
    raise NotImplementedError(f"尚未实现规则运算符: {condition.operator}")


def _resolve_hit_outcome(outcome: RuleOutcome, value: object) -> RuleOutcome:
    """按规则配置的 hit_outcome 选出实际处置结果。"""

    if outcome.type != "route_by_field_value":
        return outcome

    route = (outcome.routes or {}).get(str(value))
    if route is None:
        raise NotImplementedError(f"路由表缺少字段值 {value!r} 的处置结果")
    return route


__all__ = ["RuleEvaluation", "evaluate_rule"]
