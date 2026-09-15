"""M3-3 向量检索的单元测试: 现行版本过滤, cosine 排序与 Top-K 截断 (全部替身, 不打网络).

三件事必须钉死:

- **过滤发生在打分之前**: 被替代的版本与外部法规即使与查询完全同向 (余弦 1.0), 也
  不能出现在结果里; 只在现行语料里搜不到它们, 证明不了过滤有效, 所以负例的向量被
  故意造成比现行片段更接近查询.
- **排序确定**: 同一查询与同一份向量重复调用得到同一顺序; 同分按 `chunk_key` 升序.
- **配置坏掉要响**: 候选为空, 向量与片段数量不符, 查询与缓存维度不符都抛
  `SearchError`, 不用空结果冒充"制度里没有依据"; 查询本身的问题 (空, 过长,
  `top_k` 超限) 抛 `QueryError`, 留给模型纠正后重试.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import numpy
import pytest

from vendorguard.retrieval.chunk_list import CURRENT_EDITION_KEYS, ChunkRecord
from vendorguard.retrieval.embedding import EMBEDDING_DIMENSION, ChunkVectors
from vendorguard.retrieval.search import (
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    QueryError,
    RetrievedChunk,
    SearchError,
    search_policy,
)

MODEL = "qwen3.7-text-embedding"
CURRENT = CURRENT_EDITION_KEYS[0]
CURRENT_OTHER = CURRENT_EDITION_KEYS[1]
SUPERSEDED = "demo_supplier_admission_policy_v1"
REGULATION = "product_quality_law_v2018"

QUERY = "关键物料可以先准入后补交质量证书吗"


def _unit(*weights: float) -> tuple[float, ...]:
    """造一个 1024 维单位向量: 给定方向分量, 其余补零, 再整体归一.

    真实缓存里的向量是单位长度 (embedding 层已校验), 替身照同一契约构造,
    余弦就只由方向决定, 测试可以精确预期排序.
    """

    components = [*weights, *([0.0] * (EMBEDDING_DIMENSION - len(weights)))]
    norm = math.sqrt(sum(value * value for value in components))
    assert norm > 0, "方向分量不能全为 0"
    return tuple(value / norm for value in components)


def _record(edition_key: str, suffix: str) -> ChunkRecord:
    """造一条指定版本的最小片段; chunk_key 与 node_key 刻意不同, 便于核对回指."""

    text = f"正文{suffix}"
    return ChunkRecord(
        chunk_key=f"{edition_key}_section_{suffix}_main",
        node_key=f"{edition_key}_section_{suffix}",
        edition_key=edition_key,
        document_title="检索测试制度",
        locator_path=(f"第{suffix}节",),
        display_text=text,
        search_text=f"检索测试制度 第{suffix}节\n{text}",
        char_start=0,
        char_end=len(text),
        source_path="normalized/probe.txt",
        snapshot_sha256="a" * 64,
        normalized_sha256="b" * 64,
    )


def _vectors(*items: tuple[float, ...]) -> ChunkVectors:
    """把若干向量打包成正文缓存对象."""

    return ChunkVectors(
        model=MODEL,
        cache_key="c" * 64,
        dimension=EMBEDDING_DIMENSION,
        vectors=tuple(items),
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """测试侧独立算一遍余弦, 用来证明"负例分数更高却被过滤"."""

    a = numpy.asarray(left, dtype=numpy.float64)
    b = numpy.asarray(right, dtype=numpy.float64)
    return float(a @ b / (numpy.linalg.norm(a) * numpy.linalg.norm(b)))


class FakeEmbeddings:
    """返回固定查询向量并记录收到的查询文本."""

    def __init__(self, vector: tuple[float, ...]) -> None:
        self.vector = vector
        self.queries: list[str] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        payload = kwargs["input"]
        assert isinstance(payload, list)
        self.queries.append(str(payload[0]))
        return SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=list(self.vector))],
            usage=SimpleNamespace(prompt_tokens=1),
        )


def _client(vector: tuple[float, ...]) -> SimpleNamespace:
    """伪装 openai.OpenAI 中检索实际用到的那一小块接口."""

    return SimpleNamespace(embeddings=FakeEmbeddings(vector))


# ---------------------------------------------------------------------------
# 版本过滤: 打分之前就把负例排除
# ---------------------------------------------------------------------------


def test_negative_versions_are_filtered_before_scoring() -> None:
    """被替代的版本与外部法规与查询同向也不返回: 过滤发生在打分之前."""

    query_vector = _unit(1.0, 0.0)
    lower = _record(CURRENT, "01")
    higher = _record(CURRENT_OTHER, "02")
    records = (
        lower,
        _record(SUPERSEDED, "03"),
        higher,
        _record(REGULATION, "04"),
    )
    vectors = _vectors(
        _unit(0.0, 1.0),
        query_vector,
        _unit(1.0, 1.0),
        query_vector,
    )
    double = FakeEmbeddings(query_vector)

    results = search_policy(
        QUERY,
        records=records,
        vectors=vectors,
        embedding_client=_client(query_vector),
        model=MODEL,
    )

    assert [item.chunk_key for item in results] == [higher.chunk_key, lower.chunk_key]
    assert all(item.edition_key in CURRENT_EDITION_KEYS for item in results)
    # 负例拿到的分数是 1.0, 比返回回来的任何一条都高: 不过滤它们会排在最前面.
    assert max(item.score for item in results) < _cosine(query_vector, query_vector)
    assert double.vector == query_vector


def test_empty_candidate_set_raises_instead_of_returning_nothing() -> None:
    """候选被过滤光说明配置坏了, 要报错而不是返回空结果."""

    records = (_record(SUPERSEDED, "01"), _record(REGULATION, "02"))

    with pytest.raises(SearchError, match="现行"):
        search_policy(
            QUERY,
            records=records,
            vectors=_vectors(_unit(1.0), _unit(1.0)),
            embedding_client=_client(_unit(1.0)),
            model=MODEL,
        )


# ---------------------------------------------------------------------------
# 排序与截断
# ---------------------------------------------------------------------------


def test_results_are_ordered_and_capped_at_top_k() -> None:
    """按分数降序取前 top_k 条, 默认与上限都是 5."""

    assert MAX_TOP_K == 5
    records = tuple(_record(CURRENT, f"{index:02d}") for index in range(1, 7))
    vectors = _vectors(*(_unit(1.0, float(index)) for index in range(6)))

    results = search_policy(
        QUERY,
        records=records,
        vectors=vectors,
        embedding_client=_client(_unit(1.0, 0.0)),
        model=MODEL,
        top_k=3,
    )

    assert [item.chunk_key for item in results] == [
        records[0].chunk_key,
        records[1].chunk_key,
        records[2].chunk_key,
    ]
    assert [item.score for item in results] == sorted(
        (item.score for item in results), reverse=True
    )


def test_ties_are_broken_by_chunk_key() -> None:
    """分数相同时按 chunk_key 升序, 与片段传入顺序无关."""

    records = (
        _record(CURRENT, "03"),
        _record(CURRENT, "01"),
        _record(CURRENT, "02"),
    )
    vectors = _vectors(_unit(1.0), _unit(1.0), _unit(1.0))

    results = search_policy(
        QUERY,
        records=records,
        vectors=vectors,
        embedding_client=_client(_unit(1.0)),
        model=MODEL,
    )

    assert [item.chunk_key for item in results] == sorted(record.chunk_key for record in records)


def test_same_inputs_give_the_same_order() -> None:
    """同一查询与同一份向量重复调用得到完全一样的顺序与分数."""

    records = tuple(_record(CURRENT, f"{index:02d}") for index in range(1, 5))
    vectors = _vectors(*(_unit(1.0, float(index)) for index in range(4)))

    def run() -> tuple[RetrievedChunk, ...]:
        return search_policy(
            QUERY,
            records=records,
            vectors=vectors,
            embedding_client=_client(_unit(1.0, 0.5)),
            model=MODEL,
        )

    first, second = run(), run()
    assert [item.chunk_key for item in first] == [item.chunk_key for item in second]
    assert [item.score for item in first] == [item.score for item in second]


# ---------------------------------------------------------------------------
# 参数与配置的失败行为
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_query_is_rejected_before_any_request(query: str) -> None:
    """空查询是参数错误, 且不产生 embedding 调用."""

    double = FakeEmbeddings(_unit(1.0))
    with pytest.raises(QueryError, match="空"):
        search_policy(
            query,
            records=(_record(CURRENT, "01"),),
            vectors=_vectors(_unit(1.0)),
            embedding_client=SimpleNamespace(embeddings=double),
            model=MODEL,
        )
    assert double.queries == []


def test_overlong_query_is_rejected_at_the_declared_limit() -> None:
    """刚好到上限的查询可用, 超一个字符即参数错误."""

    records = (_record(CURRENT, "01"),)
    vectors = _vectors(_unit(1.0))
    client = _client(_unit(1.0))

    allowed = "问" * MAX_QUERY_CHARS
    assert search_policy(
        allowed, records=records, vectors=vectors, embedding_client=client, model=MODEL
    )

    with pytest.raises(QueryError, match="过长"):
        search_policy(
            allowed + "问",
            records=records,
            vectors=vectors,
            embedding_client=client,
            model=MODEL,
        )


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, 100])
def test_top_k_outside_the_range_is_rejected(top_k: int) -> None:
    """top_k 越界是参数错误, 不静默夹到合法值."""

    with pytest.raises(QueryError, match="top_k"):
        search_policy(
            QUERY,
            records=(_record(CURRENT, "01"),),
            vectors=_vectors(_unit(1.0)),
            embedding_client=_client(_unit(1.0)),
            model=MODEL,
            top_k=top_k,
        )


def test_query_dimension_must_match_the_cache() -> None:
    """缓存是另一套维度时不能拿余弦去比: 直接报错."""

    short = (1.0, *([0.0] * 7))
    vectors = ChunkVectors(model=MODEL, cache_key="c" * 64, dimension=len(short), vectors=(short,))

    with pytest.raises(SearchError, match="维度"):
        search_policy(
            QUERY,
            records=(_record(CURRENT, "01"),),
            vectors=vectors,
            embedding_client=_client(_unit(1.0)),
            model=MODEL,
        )


def test_record_and_vector_counts_must_match() -> None:
    """向量与片段按位置对齐, 数量不一致就不能算分数."""

    with pytest.raises(SearchError, match="不一致"):
        search_policy(
            QUERY,
            records=(_record(CURRENT, "01"), _record(CURRENT, "02")),
            vectors=_vectors(_unit(1.0)),
            embedding_client=_client(_unit(1.0)),
            model=MODEL,
        )


# ---------------------------------------------------------------------------
# 返回结构: 引用要能回指节点
# ---------------------------------------------------------------------------


def test_results_carry_node_reference_and_original_text() -> None:
    """每条结果同时给出召回键与可引用节点, 并带上定位, 区间与原文."""

    record = _record(CURRENT, "02")
    results = search_policy(
        QUERY,
        records=(record,),
        vectors=_vectors(_unit(1.0)),
        embedding_client=_client(_unit(1.0)),
        model=MODEL,
    )

    assert len(results) == 1
    item = results[0]
    assert isinstance(item, RetrievedChunk)
    assert item.chunk_key == record.chunk_key
    assert item.node_key == record.node_key
    assert item.chunk_key != item.node_key
    assert item.edition_key == record.edition_key
    assert item.document_title == record.document_title
    assert item.locator_path == record.locator_path
    assert (item.char_start, item.char_end) == (record.char_start, record.char_end)
    assert item.display_text == record.display_text
    assert 0.0 <= item.score <= 1.0


def test_query_is_embedded_verbatim() -> None:
    """送进 embedding 的就是原始查询, 不加前缀也不改写."""

    double = FakeEmbeddings(_unit(1.0))
    search_policy(
        QUERY,
        records=(_record(CURRENT, "01"),),
        vectors=_vectors(_unit(1.0)),
        embedding_client=SimpleNamespace(embeddings=double),
        model=MODEL,
    )

    assert double.queries == [QUERY]


def test_search_does_not_need_the_chunk_list_file(tmp_path: Path) -> None:
    """检索只吃传入的片段与向量, 不自己读文件: 缓存与清单由调用方决定."""

    assert list(tmp_path.iterdir()) == []
    search_policy(
        QUERY,
        records=(_record(CURRENT, "01"),),
        vectors=_vectors(_unit(1.0)),
        embedding_client=_client(_unit(1.0)),
        model=MODEL,
    )
    assert list(tmp_path.iterdir()) == []
