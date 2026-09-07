"""定义并加载一期固定检索评测集。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

EvaluationKind = Literal["answerable", "no_answer", "adversarial_negative"]
ExpectedOutcome = Literal["answerable", "no_answer"]
ApplicabilityCondition = Literal[
    "product_in_ccc_directory",
    "product_requires_certification",
]


class RetrievalEvaluationLoadError(ValueError):
    """评测集文件无法读取或未通过 Schema 校验时抛出的配置错误。"""


class EvaluationModel(BaseModel):
    """评测集模型的公共基类, 拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CitationTarget(EvaluationModel):
    """描述一个可被检索结果命中的版本化引用目标。"""

    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    locator: tuple[str, ...] = Field(min_length=1)

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """定位符各层均不能为空白文本。"""

        if any(not part.strip() for part in value):
            raise ValueError("定位符不能包含空白层级")
        return value


class RequiredEvidenceSlot(EvaluationModel):
    """描述一个答案成立所必需的证据槽位。"""

    slot_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    targets: tuple[CitationTarget, ...] = Field(min_length=1)


class RetrievalEvaluationCase(EvaluationModel):
    """描述一条固定且可复现的检索评测题。"""

    case_id: str = Field(pattern=r"^rag_phase1_[a-z][a-z0-9_]*$")
    query: str = Field(min_length=1)
    as_of: date
    kind: EvaluationKind
    expected_outcome: ExpectedOutcome
    required_slots: tuple[RequiredEvidenceSlot, ...] = ()
    forbidden_edition_keys: tuple[str, ...] = ()
    required_conditions: tuple[ApplicabilityCondition, ...] = ()

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """查询文本不得只包含空白字符。"""

        if not value.strip():
            raise ValueError("查询文本不能为空")
        return value

    @field_validator("forbidden_edition_keys")
    @classmethod
    def validate_forbidden_edition_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """禁止版本必须符合键格式且不可重复。"""

        if len(value) != len(set(value)):
            raise ValueError("forbidden_edition_keys 不能包含重复版本")

        for edition_key in value:
            if not edition_key or not edition_key.replace("_", "").isalnum():
                raise ValueError("forbidden_edition_keys 包含非法版本键")

        return value

    @model_validator(mode="after")
    def validate_case_shape(self) -> RetrievalEvaluationCase:
        """校验题型、预期结论、证据槽位与禁止版本之间的关系。"""

        if self.kind == "answerable" and self.expected_outcome != "answerable":
            raise ValueError("answerable 题必须期望 answerable 结论")

        if self.kind == "no_answer" and self.expected_outcome != "no_answer":
            raise ValueError("no_answer 题必须期望 no_answer 结论")

        if self.expected_outcome == "answerable" and not self.required_slots:
            raise ValueError("answerable 题必须声明至少一个必需证据槽位")

        if self.expected_outcome == "no_answer" and self.required_slots:
            raise ValueError("no_answer 题不得声明必需证据槽位")

        slot_keys = [slot.slot_key for slot in self.required_slots]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError("required_slots 的 slot_key 不能重复")

        target_edition_keys = {
            target.edition_key for slot in self.required_slots for target in slot.targets
        }
        overlap = target_edition_keys.intersection(self.forbidden_edition_keys)
        if overlap:
            raise ValueError(f"金标准版本不能同时被禁止: {sorted(overlap)}")

        if len(self.required_conditions) != len(set(self.required_conditions)):
            raise ValueError("required_conditions 不能包含重复条件")

        return self


class RetrievalEvaluationSet(EvaluationModel):
    """描述一期冻结的检索评测集。"""

    schema_version: Literal["1.0"]
    dataset_key: Literal["rag_phase1"]
    cases: tuple[RetrievalEvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> RetrievalEvaluationSet:
        """确保每条评测题都有唯一标识。"""

        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("评测题 case_id 不能重复")
        return self


def load_retrieval_evaluation_set(path: str | Path) -> RetrievalEvaluationSet:
    """使用 UTF-8 读取并校验固定检索评测集。"""

    evaluation_path = Path(path)
    try:
        payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalEvaluationLoadError(f"无法读取检索评测集: {evaluation_path}") from exc

    if not isinstance(payload, dict):
        raise RetrievalEvaluationLoadError(f"检索评测集顶层必须是对象: {evaluation_path}")

    try:
        return RetrievalEvaluationSet.model_validate(payload)
    except ValidationError as exc:
        raise RetrievalEvaluationLoadError(
            f"检索评测集未通过 Schema 校验: {evaluation_path}"
        ) from exc


__all__ = [
    "ApplicabilityCondition",
    "CitationTarget",
    "EvaluationKind",
    "ExpectedOutcome",
    "RequiredEvidenceSlot",
    "RetrievalEvaluationCase",
    "RetrievalEvaluationLoadError",
    "RetrievalEvaluationSet",
    "load_retrieval_evaluation_set",
]
