"""定义检索资格过滤契约, 使排序算法无法绕过版本和适用性约束。"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import bindparam, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.evidence.persistence import (
    KnowledgeEditionRecord,
    KnowledgeNodeRecord,
    KnowledgeRole,
    RetrievalChunkRecord,
)
from vendorguard.evidence.schema import NodeType

RetrievalIntent = Literal[
    "direct_policy",
    "background",
    "conditional_reference",
    "version_replay",
]
RetrievalStrategy = Literal["structured", "bm25", "vector"]

_EMBEDDING_DIMENSIONS = 1024

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


class RetrievedChunk(RetrievalModel):
    """通过资格过滤并由单一检索策略排序后的检索投影。"""

    strategy: RetrievalStrategy
    score: float
    chunk_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    node_type: NodeType
    locator: tuple[str, ...] = Field(min_length=1)
    display_text: str = Field(min_length=1)
    role: KnowledgeRole
    effective_from: date
    effective_until: date | None = None


class PostgresRetrievalAdapter:
    """在单一接口后封装三种数据库召回, 并先执行不可绕过的资格过滤。"""

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        query: RetrievalQuery,
        strategy: RetrievalStrategy,
        limit: int = 20,
        query_embedding: tuple[float, ...] | None = None,
    ) -> tuple[RetrievedChunk, ...]:
        """返回单策略候选, 结果已按版本、角色和案件前提过滤且分数仅供排序。"""

        statement = self._statement(
            query=query,
            strategy=strategy,
            limit=limit,
            query_embedding=query_embedding,
        )
        result = await session.execute(statement)
        rows = result.mappings()
        return tuple(
            RetrievedChunk.model_validate({**dict(row), "strategy": strategy}) for row in rows
        )

    def _statement(
        self,
        *,
        query: RetrievalQuery,
        strategy: RetrievalStrategy,
        limit: int,
        query_embedding: tuple[float, ...] | None,
    ) -> Any:
        """构建参数化 SQL, 三种排序策略共享完全相同的资格过滤谓词。"""

        if limit < 1:
            raise ValueError("limit 必须大于 0")
        if strategy == "vector":
            if query_embedding is None:
                raise ValueError("vector 检索必须提供查询向量")
            if len(query_embedding) != _EMBEDDING_DIMENSIONS:
                raise ValueError(f"查询向量维度必须为 {_EMBEDDING_DIMENSIONS}")
        elif query_embedding is not None:
            raise ValueError("只有 vector 检索可以提供查询向量")

        chunk = RetrievalChunkRecord
        node = KnowledgeNodeRecord
        edition = KnowledgeEditionRecord
        eligible_where = self._eligible_where(query)

        if strategy == "structured":
            score = func.strpos(chunk.search_text, query.text)
            strategy_where = chunk.search_text.contains(query.text, autoescape=True)
            order_by = (score.desc(), chunk.chunk_key)
        elif strategy == "bm25":
            score = func.paradedb.score(chunk.chunk_key)
            strategy_where = chunk.search_text.op("@@@")(query.text)
            order_by = (score.desc(), chunk.chunk_key)
        else:
            assert query_embedding is not None
            score = 1.0 - chunk.embedding.cosine_distance(list(query_embedding))
            strategy_where = chunk.embedding.is_not(None)
            order_by = (score.desc(), chunk.chunk_key)

        return (
            select(
                chunk.chunk_key.label("chunk_key"),
                chunk.edition_key.label("edition_key"),
                chunk.node_key.label("node_key"),
                node.node_type.label("node_type"),
                node.locator.label("locator"),
                chunk.display_text.label("display_text"),
                edition.role.label("role"),
                edition.effective_from.label("effective_from"),
                edition.effective_until.label("effective_until"),
                score.label("score"),
            )
            .join(node, chunk.node_key == node.node_key)
            .join(edition, chunk.edition_key == edition.edition_key)
            .where(*eligible_where, strategy_where)
            .order_by(*order_by)
            .limit(limit)
        )

    @staticmethod
    def _eligible_where(query: RetrievalQuery) -> tuple[Any, ...]:
        """构建所有策略共用的资格条件, 禁止由排序分支自行决定适用性。"""

        edition = KnowledgeEditionRecord
        conditions = bindparam(
            "established_conditions",
            value=sorted(query.established_conditions),
            type_=JSONB,
        )
        return (
            edition.role.in_(_INTENT_ROLES[query.intent]),
            edition.effective_from <= query.as_of,
            or_(edition.effective_until.is_(None), edition.effective_until >= query.as_of),
            edition.required_conditions.contained_by(conditions),
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
    "PostgresRetrievalAdapter",
    "RetrievalCandidate",
    "RetrievalIntent",
    "RetrievalQuery",
    "RetrievalStrategy",
    "RetrievedChunk",
    "filter_eligible_candidates",
]
