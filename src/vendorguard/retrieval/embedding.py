"""正文向量缓存与查询向量: 分批调用, 向量契约校验与本地缓存.

设计要点 (对应 M3 方案 5 与 9 的 M3-2):

- 送进检索的是片段的 search_text (带标题与表头上下文那一段), display_text 只用于
  展示与引用; 缓存指纹按 search_text 序列算, 正文一改就整份重建.
- 服务端行为按 2026-09-15 的探针结论处理: 单次最多 20 条 (第 21 条被拒绝), 返回项
  带 index 可回映射, 向量是单位长度. 任一条不成立都抛 EmbeddingError, 不做
  "顺手归一化一下继续用" 的就地修补——分数不再可比时排序会静默失真.
- 查询向量每次现算, 既不读也不写正文缓存: 混在一起会让"换了问题却命中旧向量"
  无法察觉.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

import numpy
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vendorguard.retrieval.chunk_list import ChunkRecord

# 探针实测 (scripts/probe_embedding.py): qwen3.7-text-embedding 输出 1024 维单位向量;
# 单次请求最多 20 条, 第 21 条被服务端以 400 "batch size is invalid" 拒绝.
EMBEDDING_DIMENSION = 1024
BATCH_SIZE = 20

# 单位范数的判定余量: 实测 1.000000, float32 往返误差在 1e-7 量级, 这里留三个
# 数量级余量, 只拦"明显没归一"的响应.
_NORM_TOLERANCE = 1e-3


class EmbeddingError(ValueError):
    """向量生成或缓存不可用时抛出的错误.

    检索配置坏掉必须与"检索没有依据"分开: 前者中断本次运行, 后者才是可以写进
    报告并交给人工核对的结论.
    """


class ChunkVectors(BaseModel):
    """正文片段的向量序列, 按下标与构建它的片段一一对应.

    cache_key 是模型名与 search_text 序列的联合指纹, 它既用于判断缓存是否失效,
    也让调用方可以证明手上这份向量确实对应当前清单.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimension: int = Field(ge=1)
    vectors: tuple[tuple[float, ...], ...]


def load_or_build_vectors(
    chunks: Sequence[ChunkRecord],
    *,
    client: OpenAI,
    model: str,
    cache_path: str | Path,
) -> ChunkVectors:
    """读取正文向量缓存; 缓存不存在或指纹不符时整批重建并落盘.

    指纹把模型名和整条 search_text 序列一起编进去, 所以换模型、改一条正文、
    调整片段顺序都会让缓存整体失效. 22 条的规模重建一次比维护逐条差量更可靠.
    """

    texts = [chunk.search_text for chunk in chunks]
    key = _fingerprint(model, texts)
    cached = _read_cache(cache_path)
    if cached is not None and cached.cache_key == key:
        return cached

    vectors = _embed_texts(texts, client=client, model=model)
    built = ChunkVectors(
        model=model,
        cache_key=key,
        dimension=EMBEDDING_DIMENSION,
        vectors=vectors,
    )
    _write_cache(cache_path, built)
    return built


def embed_query(query: str, *, client: OpenAI, model: str) -> tuple[float, ...]:
    """为一次检索现算查询向量, 不读也不写正文缓存."""

    if not query.strip():
        raise EmbeddingError("查询不能为空")

    vectors = _embed_texts([query], client=client, model=model)
    return vectors[0]


def _embed_texts(
    texts: Sequence[str],
    *,
    client: OpenAI,
    model: str,
) -> tuple[tuple[float, ...], ...]:
    """按 BATCH_SIZE 分批请求, 并按输入顺序拼回整段文本的向量."""

    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        vectors.extend(_embed_batch(batch, client=client, model=model))
    return tuple(vectors)


def _embed_batch(
    batch: Sequence[str],
    *,
    client: OpenAI,
    model: str,
) -> tuple[tuple[float, ...], ...]:
    """请求一批文本, 按响应里的 index 放回输入位置并逐条校验.

    不假设响应顺序与输入一致: index 缺失, 越界或重复时无法确定哪条向量属于
    哪段正文, 只能失败.
    """

    try:
        response = client.embeddings.create(model=model, input=list(batch))
    except Exception as exc:
        raise EmbeddingError(f"embedding 调用失败: {exc}") from exc

    slots: list[tuple[float, ...] | None] = [None] * len(batch)
    for item in response.data:
        index = int(item.index)
        if not 0 <= index < len(batch):
            raise EmbeddingError(f"响应 index 越界: {index}, 本批 {len(batch)} 条")
        if slots[index] is not None:
            raise EmbeddingError(f"响应 index 重复: {index}")
        slots[index] = _validated_vector(item.embedding, where=f"第 {index} 条")

    ordered: list[tuple[float, ...]] = []
    for index, slot in enumerate(slots):
        if slot is None:
            raise EmbeddingError(f"响应缺少第 {index} 条向量, 本批 {len(batch)} 条")
        ordered.append(slot)
    return tuple(ordered)


def _validated_vector(raw: Sequence[float], *, where: str) -> tuple[float, ...]:
    """校验维度, 有限值与单位范数, 三条都是探针确认过的服务端契约."""

    vector = tuple(float(value) for value in raw)
    if len(vector) != EMBEDDING_DIMENSION:
        raise EmbeddingError(f"{where} 维度不符: {len(vector)}, 期望 {EMBEDDING_DIMENSION}")

    array = numpy.asarray(vector, dtype=numpy.float64)
    if not bool(numpy.isfinite(array).all()):
        raise EmbeddingError(f"{where} 含非有限值")

    norm = float(numpy.linalg.norm(array))
    if abs(norm - 1.0) > _NORM_TOLERANCE:
        raise EmbeddingError(f"{where} 未按契约做 L2 归一: 范数 {norm:.6f}")
    return vector


def _fingerprint(model: str, texts: Sequence[str]) -> str:
    """算出模型名与正文序列的联合指纹, 作为缓存是否可复用的唯一依据."""

    digest = sha256()
    digest.update(model.encode("utf-8"))
    for text in texts:
        digest.update(b"\x00")
        digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def _read_cache(path: str | Path) -> ChunkVectors | None:
    """读取缓存; 文件不存在返回 None, 内容损坏直接报错.

    损坏不等于过期: 过期(指纹不符)按设计重建, 损坏说明本地状态已经不可信,
    静默重建会把一个真问题盖掉.
    """

    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EmbeddingError(f"向量缓存无法读取: {target}") from exc
    try:
        return ChunkVectors.model_validate_json(raw)
    except ValidationError as exc:
        raise EmbeddingError(f"向量缓存已损坏, 删除后重跑即可重建: {target}") from exc


def _write_cache(path: str | Path, vectors: ChunkVectors) -> None:
    """把向量写成单行 UTF-8 JSON; 换行固定 LF, 目录不存在时先建."""

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(vectors.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        raise EmbeddingError(f"向量缓存无法写入: {target}") from exc


__all__ = [
    "BATCH_SIZE",
    "EMBEDDING_DIMENSION",
    "ChunkVectors",
    "EmbeddingError",
    "embed_query",
    "load_or_build_vectors",
]
