"""单路向量检索: 现行版本过滤, 精确 cosine 排序与 Top-K 截断.

设计要点 (对应 M3 方案 3.2, 3.3, 7, 8):

- 候选范围由程序侧的现行白名单决定, 而且**过滤发生在打分之前**: 被替代的版本与
  外部法规哪怕与查询完全同向也不会进入结果, 模型无法用查询词或参数扩大范围.
- 相似度只用于排序, 不设阈值: 分数跨请求不可比, 判定一律基于排名与引用核对.
  score 留在返回结构里供诊断与评测, 不进入交给模型的工具结果 (方案 7 的输出里
  没有分数), 也不参与任何阈值判断.
- 返回的是召回片段与它回指的节点: chunk_key 只服务召回, node_key 才是最终引用单位.
- 检索配置坏掉 (候选被过滤光, 向量与片段数量不符, 查询与缓存维度不符) 一律报错,
  不允许用空结果冒充"制度里没有依据".
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from vendorguard.retrieval.chunk_list import CURRENT_EDITION_KEYS, ChunkRecord
from vendorguard.retrieval.embedding import ChunkVectors, embed_query

DEFAULT_TOP_K = 5
MAX_TOP_K = 5

# 查询是问题而不是材料: 超过这个长度按参数错误处理, 免得整段正文被当成查询塞进来.
MAX_QUERY_CHARS = 500


class QueryError(ValueError):
    """查询参数不合法 (空, 过长, top_k 越界) 时抛出的错误.

    这类错误可以回到模型纠正后重试, 与"检索本身坏了"要分开处理.
    """


class SearchError(ValueError):
    """检索配置或数据不可用时抛出的错误, 属于本次运行失败.

    候选为空或向量对不上时返回空结果, 会让"检索坏了"和"制度真的没规定"变成同一
    件事, 所以这里直接中断.
    """


class RetrievedChunk(BaseModel):
    """一条检索结果: 召回片段与它回指的节点.

    locator_path 的首层就是展示用的标题 (工具结果里的 title); chunk_key 只用于召回,
    最终引用必须落在 node_key 上. score 仅作排序与诊断, 跨请求不可比.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_key: str = Field(min_length=1)
    node_key: str = Field(min_length=1)
    edition_key: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    locator_path: tuple[str, ...] = Field(min_length=1)
    display_text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)


def search_policy(
    query: str,
    *,
    records: Sequence[ChunkRecord],
    vectors: ChunkVectors,
    embedding_client: OpenAI,
    model: str,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[RetrievedChunk, ...]:
    """在现行制度片段里检索与查询最相关的 top_k 条.

    顺序固定: 校验参数 -> 生成查询向量并核对维度 -> 按现行白名单过滤候选 ->
    精确 cosine 打分 -> 分数降序 (同分按 chunk_key) -> 取前 top_k.
    """

    _validate_request(query, records=records, vectors=vectors, top_k=top_k)

    query_vector = embed_query(query, client=embedding_client, model=model)
    if len(query_vector) != vectors.dimension:
        raise SearchError(
            f"查询向量维度 {len(query_vector)} 与正文缓存维度 {vectors.dimension} 不符"
        )

    candidates = _current_candidates(records, vectors)
    scored = [_scored_chunk(record, vector, query_vector) for record, vector in candidates]
    scored.sort(key=lambda item: (-item.score, item.chunk_key))
    return tuple(scored[:top_k])


def _validate_request(
    query: str,
    *,
    records: Sequence[ChunkRecord],
    vectors: ChunkVectors,
    top_k: int,
) -> None:
    """在任何网络请求之前校验参数与数据形状."""

    if not query.strip():
        raise QueryError("查询不能为空")
    if len(query) > MAX_QUERY_CHARS:
        raise QueryError(f"查询过长: {len(query)} 字符, 上限 {MAX_QUERY_CHARS}")
    if not 1 <= top_k <= MAX_TOP_K:
        raise QueryError(f"top_k 超出范围: {top_k}, 允许 1 到 {MAX_TOP_K}")
    if len(records) != len(vectors.vectors):
        raise SearchError(
            f"向量条数 {len(vectors.vectors)} 与片段条数 {len(records)} 不一致, "
            "缓存与清单不是同一次构建"
        )


def _current_candidates(
    records: Sequence[ChunkRecord],
    vectors: ChunkVectors,
) -> list[tuple[ChunkRecord, tuple[float, ...]]]:
    """按现行白名单过滤候选; 打分只发生在过滤之后."""

    current = set(CURRENT_EDITION_KEYS)
    candidates = [
        (record, vector)
        for record, vector in zip(records, vectors.vectors, strict=True)
        if record.edition_key in current
    ]
    if not candidates:
        raise SearchError("现行制度没有可用候选: 片段清单与向量缓存都不在现行范围")
    return candidates


def _scored_chunk(
    record: ChunkRecord,
    vector: tuple[float, ...],
    query_vector: tuple[float, ...],
) -> RetrievedChunk:
    """算一条片段与查询的余弦相似度, 并组装成带回指信息的检索结果."""

    body = numpy.asarray(vector, dtype=numpy.float64)
    query = numpy.asarray(query_vector, dtype=numpy.float64)
    score = float(body @ query / (numpy.linalg.norm(body) * numpy.linalg.norm(query)))

    return RetrievedChunk(
        chunk_key=record.chunk_key,
        node_key=record.node_key,
        edition_key=record.edition_key,
        document_title=record.document_title,
        locator_path=record.locator_path,
        display_text=record.display_text,
        char_start=record.char_start,
        char_end=record.char_end,
        score=score,
    )


__all__ = [
    "DEFAULT_TOP_K",
    "MAX_QUERY_CHARS",
    "MAX_TOP_K",
    "QueryError",
    "RetrievedChunk",
    "SearchError",
    "search_policy",
]
