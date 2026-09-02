"""定义并加载版本化 VendorGuard 规则策略。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

PolicyScope = Literal["supplier_admission", "procurement_exception"]
PolicyStatus = Literal["draft", "approved", "retired"]
RuleOperator = Literal["equals", "in", "less_than", "greater_than"]
RuleAction = Literal[
    "block",
    "request_documents",
    "require_manual_verification",
    "require_approval",
    "require_exception_approval",
    "create_manual_review",
]
RuleResult = Literal["hit", "not_hit", "not_applicable"]
RuleValueType = Literal["integer_days", "decimal_percent"]
ApprovalRole = Literal["procurement_manager", "quality_manager"]


class PolicyLoadError(ValueError):
    """策略文件无法读取或未通过 Schema 校验时抛出的配置错误。"""


class PolicyModel(BaseModel):
    """所有规则配置模型的公共基类, 禁止未声明的额外字段。"""

    model_config = ConfigDict(extra="forbid")


class RuleCondition(PolicyModel):
    """描述一条规则要检查的字段、运算符和比较值。"""

    field: str
    operator: RuleOperator
    value: Any
    value_type: RuleValueType | None = None


class RulePreconditions(PolicyModel):
    """描述规则执行前必须同时满足的前置条件集合。"""

    all: list[RuleCondition]


class RuleOutcome(PolicyModel):
    """描述规则命中、输入缺失或前置条件失败后的处置动作。"""

    action: RuleAction | None = None
    type: Literal["route_by_field_value"] | None = None
    routes: dict[str, RuleOutcome] | None = None
    blocking: bool | None = None
    target_case_status: Literal["pending_documents"] | None = None
    required_roles: list[ApprovalRole] | None = None
    bind_to: Literal["purchase_requisition"] | None = None
    reusable_for_other_prs: bool | None = None
    handled_with_rule_id: str | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> RuleOutcome:
        """校验普通动作结果与按字段路由结果的结构是否一致。"""

        if self.type == "route_by_field_value":
            if not self.routes:
                raise ValueError("按字段值路由的结果必须配置 routes")
            if self.action is not None:
                raise ValueError("按字段值路由的结果不能同时配置 action")
        elif self.routes is not None:
            raise ValueError("只有 type=route_by_field_value 才能配置 routes")
        elif self.action is None:
            raise ValueError("普通规则结果必须配置 action")
        return self


class EvidenceRequirement(PolicyModel):
    """描述规则所需的材料证据或结构化事实证据。"""

    document_type: str | None = None
    location_type: str | None = None
    fact_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> EvidenceRequirement:
        """校验证据要求只能指向材料或事实中的一种来源。"""

        if (self.document_type is None) == (self.fact_type is None):
            raise ValueError("证据要求必须且只能配置 document_type 或 fact_type")
        if self.document_type is not None and self.location_type is None:
            raise ValueError("材料证据必须配置 location_type")
        if self.fact_type is not None and self.location_type is not None:
            raise ValueError("事实证据不能配置 location_type")
        return self


class RuleDefinition(PolicyModel):
    """描述一条完整规则及其适用范围、条件、结果和证据要求。"""

    id: str
    name: str
    scopes: list[PolicyScope]
    input_fields: list[str]
    condition: RuleCondition
    hit_outcome: RuleOutcome
    on_missing_input: RuleOutcome
    preconditions: RulePreconditions | None = None
    on_precondition_failed: RuleOutcome | None = None
    evidence_requirements: list[EvidenceRequirement]
    notes: list[str] | None = None


class ImplementationScope(PolicyModel):
    """描述规则在当前十天 MVP 中的启用与延期范围。"""

    status: Literal["scoped_for_10_day_mvp"]
    enabled_rule_ids: list[str]
    deferred_rule_ids: list[str]
    notes: list[str]


class EvaluationContract(PolicyModel):
    """描述规则引擎允许使用的运算符、动作和通用比较约定。"""

    allowed_operators: list[RuleOperator]
    allowed_actions: list[RuleAction]
    rule_results: list[RuleResult]
    decimal_comparison: str
    date_boundary: str
    missing_prerequisite: str
    approval_merge: str


class PolicyDocument(PolicyModel):
    """描述一个可版本化、生效时间明确的完整规则策略文档。"""

    schema_version: Literal["1.0"]
    policy_id: str
    version: str
    status: PolicyStatus
    effective_from: date
    effective_until: date | None = None
    simulation_only: bool
    description: str
    source_document: str
    implementation_scope: ImplementationScope
    evaluation_contract: EvaluationContract
    rules: list[RuleDefinition]

    @model_validator(mode="after")
    def validate_rule_registry(self) -> PolicyDocument:
        """校验规则 ID 唯一, 并确保启用与延期清单完整覆盖规则。"""

        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("规则 ID 不能重复")

        enabled = set(self.implementation_scope.enabled_rule_ids)
        deferred = set(self.implementation_scope.deferred_rule_ids)
        configured = enabled | deferred
        if enabled & deferred:
            raise ValueError("启用规则和延期规则不能重复")
        if configured != set(rule_ids):
            raise ValueError("启用规则和延期规则必须完整覆盖所有规则")
        return self


def load_policy(path: str | Path) -> PolicyDocument:
    """使用 UTF-8 读取并校验 YAML 规则策略文档。"""

    policy_path = Path(path)
    try:
        raw_text = policy_path.read_text(encoding="utf-8")
        raw_data = yaml.safe_load(raw_text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PolicyLoadError(f"无法读取规则策略文件: {policy_path}") from exc

    try:
        return PolicyDocument.model_validate(raw_data)
    except ValidationError as exc:
        raise PolicyLoadError(f"规则策略文件未通过 Schema 校验: {policy_path}") from exc


__all__ = ["PolicyDocument", "PolicyLoadError", "load_policy"]
