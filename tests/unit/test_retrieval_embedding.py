"""M3-2 embedding 客户端与本地缓存的单元测试 (全部用替身, 不打网络).

2026-09-15 真实探针实测 (`scripts/probe_embedding.py`, 一次调用):

- 模型 `qwen3.7-text-embedding` 可用, 维度 1024;
- 返回向量已做 L2 归一 (范数 1.000000);
- 响应项带 `index`, 与输入同序, 可以按 index 回映射;
- 单次请求的输入条数上限是 20: 21 条被服务端拒绝 (400
  "batch size is invalid, it should not be larger than 20")。

下面钉住的是本模块自己负责的部分: 分批不超过 20, 按 index 回映射, 向量契约
(维度, 有限值, 单位范数) 不满足时明确失败, 缓存按"模型名 + 正文指纹"失效,
以及查询向量不进正文缓存.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from vendorguard.retrieval.chunk_list import ChunkRecord
from vendorguard.retrieval.embedding import (
    BATCH_SIZE,
    EMBEDDING_DIMENSION,
    ChunkVectors,
    EmbeddingError,
    embed_query,
    load_or_build_vectors,
)

CACHE_PATH = Path("embedding_v1.json")
MODEL = "qwen3.7-text-embedding"


def _unit_vector(text: str, *, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """按文本内容生成确定的单位向量: 两个分量放 1/sqrt(2), 位置由正文哈希决定.

    探针确认服务端返回的是单位向量, 替身也照这个契约返回; 分量位置编码了
    文本身份, 因此"第 i 个输出对应第 i 个输入"可以被逐分量核对, 而不是只数条数.
    """

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    first = digest[0] % dimension
    second = (first + 1 + digest[1] % (dimension - 1)) % dimension
    vector = [0.0] * dimension
    value = 1 / math.sqrt(2)
    vector[first] = value
    vector[second] = value
    return vector


def _record(index: int, text: str) -> ChunkRecord:
    """造一条最小可用片段, 展示文本与检索文本刻意不同, 用来验证送入检索的是哪个."""

    return ChunkRecord(
        chunk_key=f"demo_probe_section_{index}_main",
        node_key=f"demo_probe_section_{index}",
        edition_key="demo_supplier_admission_policy_v2",
        document_title="探针制度",
        locator_path=(f"第{index}节",),
        display_text=text,
        search_text=f"探针制度 第{index}节\n{text}",
        char_start=0,
        char_end=len(text),
        source_path="normalized/demo_probe.txt",
        snapshot_sha256="a" * 64,
        normalized_sha256="b" * 64,
    )


def _records(count: int) -> tuple[ChunkRecord, ...]:
    """造 count 条互不相同的片段."""

    return tuple(_record(index, f"探针正文第{index}条") for index in range(1, count + 1))


class FakeEmbeddings:
    """记录每次请求, 按正文哈希返回可预测的单位向量.

    damage 用来制造坏响应 (维度不符, 非单位范数, 非有限值, index 越界或重复),
    这些用例全部停在替身层, 不产生真实请求.
    """

    def __init__(
        self,
        *,
        dimension: int = EMBEDDING_DIMENSION,
        reverse: bool = False,
        damage: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.dimension = dimension
        self.reverse = reverse
        self.damage = damage
        self.error = error
        self.requests: list[list[str]] = []
        self.models: list[str] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        """伪装 client.embeddings.create, 返回带 index 的数据项与 usage."""

        model = str(kwargs["model"])
        inputs = [str(item) for item in kwargs["input"]]  # type: ignore[union-attr]
        self.requests.append(list(inputs))
        self.models.append(model)
        if self.error is not None:
            raise self.error

        items = [
            SimpleNamespace(index=index, embedding=_unit_vector(text, dimension=self.dimension))
            for index, text in enumerate(inputs)
        ]
        if self.damage == "dimension":
            items = [SimpleNamespace(index=item.index, embedding=[1.0, 0.0]) for item in items]
        elif self.damage == "norm":
            items = [
                SimpleNamespace(index=item.index, embedding=[value * 2 for value in item.embedding])
                for item in items
            ]
        elif self.damage == "not_finite":
            broken = list(items[0].embedding)
            broken[0] = math.nan
            items[0] = SimpleNamespace(index=0, embedding=broken)
        elif self.damage == "index_out_of_range":
            items[0] = SimpleNamespace(index=len(inputs) + 5, embedding=items[0].embedding)
        elif self.damage == "index_duplicated":
            items[1] = SimpleNamespace(index=0, embedding=items[1].embedding)
        if self.reverse:
            items = list(reversed(items))
        return SimpleNamespace(data=items, usage=SimpleNamespace(prompt_tokens=len(inputs)))


def _client(double: FakeEmbeddings) -> SimpleNamespace:
    """伪装 openai.OpenAI 中本模块实际用到的那一小块接口."""

    return SimpleNamespace(embeddings=double)


# ---------------------------------------------------------------------------
# 分批与回映射
# ---------------------------------------------------------------------------


def test_body_texts_are_embedded_in_batches_of_at_most_twenty(tmp_path: Path) -> None:
    """送入检索的是 search_text, 分批不超过 20 条且拼接后仍按输入顺序."""

    assert BATCH_SIZE == 20
    double = FakeEmbeddings()
    records = _records(45)

    vectors = load_or_build_vectors(
        records, client=_client(double), model=MODEL, cache_path=tmp_path / CACHE_PATH
    )

    assert [len(batch) for batch in double.requests] == [20, 20, 5]
    assert all(len(batch) <= BATCH_SIZE for batch in double.requests)
    assert [text for batch in double.requests for text in batch] == [
        record.search_text for record in records
    ]
    assert len(vectors.vectors) == 45
    assert vectors.vectors[0] == tuple(_unit_vector(records[0].search_text))


def test_out_of_order_response_is_remapped_by_index(tmp_path: Path) -> None:
    """服务端乱序返回时按 index 回映射, 不按返回顺序直接拼."""

    double = FakeEmbeddings(reverse=True)
    records = _records(3)

    vectors = load_or_build_vectors(
        records, client=_client(double), model=MODEL, cache_path=tmp_path / CACHE_PATH
    )

    assert [list(vector) for vector in vectors.vectors] == [
        _unit_vector(record.search_text) for record in records
    ]


@pytest.mark.parametrize("damage", ["index_out_of_range", "index_duplicated"])
def test_broken_index_is_rejected(tmp_path: Path, damage: str) -> None:
    """index 越界或重复说明响应与请求无法对齐, 必须失败而不是错位拼接."""

    double = FakeEmbeddings(damage=damage)
    with pytest.raises(EmbeddingError, match="index"):
        load_or_build_vectors(
            _records(3), client=_client(double), model=MODEL, cache_path=tmp_path / CACHE_PATH
        )


# ---------------------------------------------------------------------------
# 向量契约
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("damage", "hint"),
    [("dimension", "维度"), ("norm", "归一"), ("not_finite", "有限")],
)
def test_broken_vector_is_rejected(tmp_path: Path, damage: str, hint: str) -> None:
    """维度, 单位范数与有限值按探针结论校验, 不满足时明确失败."""

    double = FakeEmbeddings(damage=damage)
    with pytest.raises(EmbeddingError, match=hint):
        load_or_build_vectors(
            _records(3), client=_client(double), model=MODEL, cache_path=tmp_path / CACHE_PATH
        )


def test_embedding_failure_is_not_swallowed(tmp_path: Path) -> None:
    """接口报错原样变成 EmbeddingError, 不能返回空向量让上层以为没有依据."""

    double = FakeEmbeddings(error=RuntimeError("connection reset"))
    with pytest.raises(EmbeddingError, match="connection reset"):
        load_or_build_vectors(
            _records(2), client=_client(double), model=MODEL, cache_path=tmp_path / CACHE_PATH
        )


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def test_cache_is_reused_when_model_and_texts_are_unchanged(tmp_path: Path) -> None:
    """同一份正文与模型二次运行不重复嵌入, 读回的向量与首次逐分量一致."""

    cache_path = tmp_path / CACHE_PATH
    records = _records(4)
    first_double = FakeEmbeddings()
    first = load_or_build_vectors(
        records, client=_client(first_double), model=MODEL, cache_path=cache_path
    )
    assert len(first_double.requests) == 1

    second_double = FakeEmbeddings()
    second = load_or_build_vectors(
        records, client=_client(second_double), model=MODEL, cache_path=cache_path
    )

    assert second_double.requests == []
    assert second == first


def test_cache_is_rebuilt_when_texts_or_model_change(tmp_path: Path) -> None:
    """正文或模型任一变化都整体重建, 不做逐条差量复用."""

    cache_path = tmp_path / CACHE_PATH
    records = _records(3)
    double = FakeEmbeddings()
    client = _client(double)
    first = load_or_build_vectors(records, client=client, model=MODEL, cache_path=cache_path)

    changed = (*records[:2], _record(3, "改过的正文"))
    rebuilt = load_or_build_vectors(changed, client=client, model=MODEL, cache_path=cache_path)
    assert len(double.requests) == 2
    assert rebuilt.cache_key != first.cache_key

    other_model = "qwen3.7-other-embedding"
    reloaded = load_or_build_vectors(
        changed, client=client, model=other_model, cache_path=cache_path
    )
    assert len(double.requests) == 3
    assert double.models[-1] == other_model
    assert reloaded.cache_key != rebuilt.cache_key
    assert reloaded.model == other_model


def test_corrupt_cache_is_an_error_not_a_silent_rebuild(tmp_path: Path) -> None:
    """缓存损坏要报错: 静默重建会把"本地状态坏了"伪装成"重新算了一遍没关系"."""

    cache_path = tmp_path / CACHE_PATH
    cache_path.write_text("{这不是 JSON", encoding="utf-8")
    double = FakeEmbeddings()

    with pytest.raises(EmbeddingError, match="缓存"):
        load_or_build_vectors(
            _records(2), client=_client(double), model=MODEL, cache_path=cache_path
        )
    assert double.requests == []


def test_cache_file_records_model_and_fingerprint(tmp_path: Path) -> None:
    """落盘的缓存要能自证身份: 模型名与指纹都在文件里."""

    cache_path = tmp_path / CACHE_PATH
    load_or_build_vectors(
        _records(2), client=_client(FakeEmbeddings()), model=MODEL, cache_path=cache_path
    )

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert tuple(payload) == ("model", "cache_key", "dimension", "vectors")
    assert payload["model"] == MODEL
    assert payload["dimension"] == EMBEDDING_DIMENSION
    assert len(payload["vectors"]) == 2


# ---------------------------------------------------------------------------
# 查询向量
# ---------------------------------------------------------------------------


def test_query_vector_is_generated_per_call_and_not_cached(tmp_path: Path) -> None:
    """查询向量每次请求现算, 既不读缓存也不写正文缓存."""

    double = FakeEmbeddings()
    client = _client(double)

    first = embed_query("关键物料可以先准入后补交质量证书吗", client=client, model=MODEL)
    second = embed_query("关键物料可以先准入后补交质量证书吗", client=client, model=MODEL)

    assert len(double.requests) == 2
    assert first == second
    assert isinstance(first, tuple)
    assert len(first) == EMBEDDING_DIMENSION
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0, abs_tol=1e-9)
    assert list(tmp_path.iterdir()) == []


def test_query_vector_follows_the_same_contract(tmp_path: Path) -> None:
    """查询向量与正文向量同一套契约, 坏响应一样要失败."""

    with pytest.raises(EmbeddingError, match="维度"):
        embed_query("查询", client=_client(FakeEmbeddings(damage="dimension")), model=MODEL)


@pytest.mark.parametrize("query", ["", "   ", "\n"])
def test_blank_query_is_rejected_before_any_request(query: str) -> None:
    """空查询在发请求前就被拒绝, 不浪费一次调用也不让服务端替我们判断."""

    double = FakeEmbeddings()
    with pytest.raises(EmbeddingError, match="查询"):
        embed_query(query, client=_client(double), model=MODEL)
    assert double.requests == []


def test_chunk_vectors_alignment_is_explicit(tmp_path: Path) -> None:
    """向量序列与输入片段一一对应, 长度不同即认为缓存与清单不匹配."""

    records = _records(3)
    vectors = load_or_build_vectors(
        records, client=_client(FakeEmbeddings()), model=MODEL, cache_path=tmp_path / CACHE_PATH
    )

    assert isinstance(vectors, ChunkVectors)
    assert len(vectors.vectors) == len(records)
    assert all(len(vector) == vectors.dimension for vector in vectors.vectors)
