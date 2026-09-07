"""定义并校验可审计检索的固定离线评测集。"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vendorguard.evidence.schema import KnowledgeNode, NodeType

EvaluationKind = Literal["answerable", "no_answer", "adversarial_negative"]
ExpectedOutcome = Literal["sufficient", "insufficient"]


class EvaluationSetError(ValueError):
    """评测集文件不符合固定问题、槽位或引用定位契约时抛出。"""


class EvaluationModel(BaseModel):
    """评测数据公共基类, 拒绝未声明字段以防金标准被静默忽略。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CitationTarget(EvaluationModel):
    """金标准引用目标, 以版本、节点类型和结构定位符表达可接受节点集合。"""

    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    node_type: NodeType
    locator: tuple[str, ...] = Field(min_length=1)


class RequiredEvidenceSlot(EvaluationModel):
    """一个业务主张必须由至少一个指定版本化定位符支撑。"""

    slot_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    accepted_citations: tuple[CitationTarget, ...] = Field(min_length=1)


class RetrievalEvaluationCase(EvaluationModel):
    """固定检索问题及其时间边界、证据槽位和禁止引用。"""

    case_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: EvaluationKind
    query: str = Field(min_length=1)
    as_of: date
    expected_outcome: ExpectedOutcome
    required_slots: tuple[RequiredEvidenceSlot, ...] = ()
    prohibited_citations: tuple[CitationTarget, ...] = ()

    @model_validator(mode="after")
    def validate_case_shape(self) -> RetrievalEvaluationCase:
        """按题型约束证据充分性结论, 防止拒答题混入正向 Recall。"""

        slot_keys = [slot.slot_key for slot in self.required_slots]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError("required_slots 不能包含重复槽位")

        if self.kind == "answerable":
            if self.expected_outcome != "sufficient" or not self.required_slots:
                raise ValueError("answerable 必须声明 sufficient 和至少一个证据槽位")
            return self

        if self.kind == "no_answer":
            if self.expected_outcome != "insufficient" or self.required_slots:
                raise ValueError("no_answer 必须声明 insufficient 且不得要求证据槽位")
            if not self.prohibited_citations:
                raise ValueError("no_answer 必须列出至少一个禁止引用")
            return self

        if not self.prohibited_citations:
            raise ValueError("adversarial_negative 必须列出至少一个禁止引用")
        if self.expected_outcome == "sufficient" and not self.required_slots:
            raise ValueError("充分的对抗题必须声明证据槽位")
        if self.expected_outcome == "insufficient" and self.required_slots:
            raise ValueError("证据不足的对抗题不得声明证据槽位")
        return self


class RetrievalEvaluationSet(EvaluationModel):
    """一期固定评测集, 使用三组题分别衡量召回、拒答与对抗防护。"""

    schema_version: Literal["1.0"]
    corpus_key: Literal["vendorguard_rag_phase_1"]
    cases: tuple[RetrievalEvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_fixed_phase_one_distribution(self) -> RetrievalEvaluationSet:
        """一期固定为 15 题, 并保留三组题的最小区分度。"""

        case_keys = [case.case_key for case in self.cases]
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("评测 case_key 不能重复")
        if len(self.cases) != 15:
            raise ValueError("一期评测集必须固定为 15 题")

        counts = Counter(case.kind for case in self.cases)
        expected_counts = {
            "answerable": 6,
            "no_answer": 4,
            "adversarial_negative": 5,
        }
        if counts != expected_counts:
            raise ValueError("一期评测集题型配比必须为 6 个 answerable, 4 个 no_answer, 5 个对抗题")
        return self


def load_retrieval_evaluation_set(path: str | Path) -> RetrievalEvaluationSet:
    """读取并校验固定 JSON 评测集。"""

    evaluation_path = Path(path)
    try:
        payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationSetError(f"无法读取检索评测集: {evaluation_path}") from exc

    try:
        return RetrievalEvaluationSet.model_validate(payload)
    except ValidationError as exc:
        raise EvaluationSetError(f"检索评测集未通过 Schema 校验: {evaluation_path}") from exc


def validate_citation_targets(
    evaluation_set: RetrievalEvaluationSet,
    nodes: Iterable[KnowledgeNode],
) -> None:
    """确认每个金标准和禁止引用都能解析为当前冻结语料中的真实节点。"""

    available_targets = {(node.edition_key, node.node_type, node.locator) for node in nodes}

    for case in evaluation_set.cases:
        targets = [target for slot in case.required_slots for target in slot.accepted_citations]
        targets.extend(case.prohibited_citations)
        for target in targets:
            identity = (target.edition_key, target.node_type, target.locator)
            if identity not in available_targets:
                raise EvaluationSetError(
                    f"评测题 {case.case_key} 引用了不存在的金标准节点: "
                    f"{target.edition_key} / {' > '.join(target.locator)}"
                )


__all__ = [
    "CitationTarget",
    "EvaluationSetError",
    "RequiredEvidenceSlot",
    "RetrievalEvaluationCase",
    "RetrievalEvaluationSet",
    "load_retrieval_evaluation_set",
    "validate_citation_targets",
]
