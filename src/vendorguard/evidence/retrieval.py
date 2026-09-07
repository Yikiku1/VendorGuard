"""定义检索资格过滤契约, 使排序算法无法绕过版本和适用性约束。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vendorguard.evidence.persistence import KnowledgeRole

RetrievalIntent = Literal[
    "direct_policy",
    "background",
    "conditional_reference",
    "version_replay",
]

_INTENT_ROLES: dict[RetrievalIntent, frozenset[KnowledgeRole]] = {
    "direct_policy": frozenset({KnowledgeRole.DIRECT_POLICY}),
    "background": frozenset(
        {
            KnowledgeRole.BACKGROUND,
            KnowledgeRole.BACKGROUND_AND_VERSION_NEGATIVE,
        }
    ),
    "conditional_reference": frozenset({KnowledgeRole.CONDITIONAL_REFERENCE}),
    "version_replay": frozenset(
        {
            KnowledgeRole.DIRECT_POLICY,
            KnowledgeRole.VERSION_NEGATIVE,
            KnowledgeRole.BACKGROUND_AND_VERSION_NEGATIVE,
        }
    ),
}


class RetrievalModel(BaseModel):
    """检索资格对象的公共基类, 拒绝无声明字段避免过滤条件丢失。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalQuery(RetrievalModel):
    """调用检索模块所需的最小业务上下文。"""

    text: str = Field(min_length=1)
    as_of: date
    intent: RetrievalIntent
    established_conditions: frozenset[str] = frozenset()

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """拒绝只有空白的查询, 避免空查询在所有分支退化为全库扫描。"""

        if not value.strip():
            raise ValueError("检索文本不能为空")
        return value


class RetrievalCandidate(RetrievalModel):
    """排序前的检索候选及其全部适用性元数据。"""

    chunk_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    role: KnowledgeRole
    effective_from: date
    effective_until: date | None = None
    required_conditions: frozenset[str] = frozenset()

    def is_effective_on(self, as_of: date) -> bool:
        """复用项目闭区间日期语义判断候选版本是否可在指定日期引用。"""

        return self.effective_from <= as_of and (
            self.effective_until is None or as_of <= self.effective_until
        )


def filter_eligible_candidates(
    query: RetrievalQuery,
    candidates: tuple[RetrievalCandidate, ...],
) -> tuple[RetrievalCandidate, ...]:
    """在任何排序前过滤过期、角色不符或条件尚未成立的候选。

    返回顺序严格保留调用方顺序. 这让 BM25、余弦距离和 RRF 可以在本函数之后各自排序,
    但都无法把不具备候选资格的资料重新放回结果。
    """

    allowed_roles = _INTENT_ROLES[query.intent]
    return tuple(
        candidate
        for candidate in candidates
        if candidate.role in allowed_roles
        and candidate.is_effective_on(query.as_of)
        and candidate.required_conditions <= query.established_conditions
    )


__all__ = [
    "RetrievalCandidate",
    "RetrievalIntent",
    "RetrievalQuery",
    "filter_eligible_candidates",
]
