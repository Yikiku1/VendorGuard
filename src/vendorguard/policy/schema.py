"""Typed models and loader for versioned VendorGuard policies."""

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
    """Raised when a policy file cannot be parsed or violates its schema."""


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleCondition(PolicyModel):
    field: str
    operator: RuleOperator
    value: Any
    value_type: RuleValueType | None = None


class RulePreconditions(PolicyModel):
    all: list[RuleCondition]


class RuleOutcome(PolicyModel):
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
        if self.type == "route_by_field_value":
            if not self.routes:
                raise ValueError("route_by_field_value requires routes")
            if self.action is not None:
                raise ValueError("route_by_field_value cannot define action")
        elif self.routes is not None:
            raise ValueError("routes require type=route_by_field_value")
        elif self.action is None:
            raise ValueError("rule outcome requires action")
        return self


class EvidenceRequirement(PolicyModel):
    document_type: str | None = None
    location_type: str | None = None
    fact_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> EvidenceRequirement:
        if (self.document_type is None) == (self.fact_type is None):
            raise ValueError("evidence requirement needs exactly one document_type or fact_type")
        if self.document_type is not None and self.location_type is None:
            raise ValueError("document evidence requires location_type")
        if self.fact_type is not None and self.location_type is not None:
            raise ValueError("fact evidence cannot define location_type")
        return self


class RuleDefinition(PolicyModel):
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
    status: Literal["scoped_for_10_day_mvp"]
    enabled_rule_ids: list[str]
    deferred_rule_ids: list[str]
    notes: list[str]


class EvaluationContract(PolicyModel):
    allowed_operators: list[RuleOperator]
    allowed_actions: list[RuleAction]
    rule_results: list[RuleResult]
    decimal_comparison: str
    date_boundary: str
    missing_prerequisite: str
    approval_merge: str


class PolicyDocument(PolicyModel):
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
        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule IDs must be unique")

        enabled = set(self.implementation_scope.enabled_rule_ids)
        deferred = set(self.implementation_scope.deferred_rule_ids)
        configured = enabled | deferred
        if enabled & deferred:
            raise ValueError("enabled and deferred rule IDs must be disjoint")
        if configured != set(rule_ids):
            raise ValueError("enabled and deferred rule IDs must cover every rule exactly")
        return self


def load_policy(path: str | Path) -> PolicyDocument:
    """Load and validate a UTF-8 YAML policy document."""

    policy_path = Path(path)
    try:
        raw_text = policy_path.read_text(encoding="utf-8")
        raw_data = yaml.safe_load(raw_text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PolicyLoadError(f"Unable to read policy: {policy_path}") from exc

    try:
        return PolicyDocument.model_validate(raw_data)
    except ValidationError as exc:
        raise PolicyLoadError(f"Invalid policy schema: {policy_path}") from exc


__all__ = ["PolicyDocument", "PolicyLoadError", "load_policy"]
