"""在冻结语料上做本地检索, 并按参考日期过滤失效版本.

设计要点:

- 检索单位是 `RetrievalChunk` 的 `search_text`, 引用单位仍是它回指的
  `KnowledgeNode`; 定位符取自节点, 与 `data/evals/rag_phase1.json` 金标准
  逐字一致, 因此检索结果可直接与评测集比对。
- 中文按字符二元组切分, 拉丁与数字按整词切分。这是无外部分词依赖的
  确定性做法, 与项目"不引入额外服务"的取舍一致。
- **版本过滤先于相关性排序**: 每个版本的 `effective_from` / `effective_until`
  由程序按参考日期判定, 失效版本根本不进入候选集。旧版制度误用是评测集
  的主要反例来源, 靠提示词约束不住, 必须由检索层结构性挡掉。
- 只做召回与引用, 不生成答案文本。结论由模型基于召回片段书写, 程序
  负责保证"引用确实存在且字面可回放"。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .evidence.node_parser import EditionParseResult, parse_frozen_corpus

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-z0-9]+")


class RetrievalError(ValueError):
    """索引构建或检索参数非法时抛出的错误。"""


class Citation(BaseModel):
    """一条可回放的引用: 版本、定位符与原文片段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    edition_key: str
    locator: tuple[str, ...]
    node_key: str
    char_start: int
    char_end: int
    body: str
    score: float

    def as_label(self) -> str:
        """渲染成人可读的引用标签, 形如 版本@第一层/第二层。"""

        return f"{self.edition_key}@{'/'.join(self.locator)}"


class RetrievalHit(BaseModel):
    """一次检索的返回: 命中片段与本次生效的版本口径。"""

    model_config = ConfigDict(extra="forbid")

    query: str
    as_of: date
    citations: list[Citation]
    effective_edition_keys: list[str]
    filtered_edition_keys: list[str]


def _tokenize(text: str) -> list[str]:
    """中文取字符二元组, 拉丁与数字取整词, 全部小写。"""

    lowered = text.lower()
    tokens = [match.group() for match in _LATIN.finditer(lowered)]
    characters = [char for char in lowered if _CJK.match(char)]
    tokens.extend(characters[index] + characters[index + 1] for index in range(len(characters) - 1))
    if len(characters) == 1:
        tokens.append(characters[0])
    return tokens


class _IndexedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_key: str
    edition_key: str
    node_key: str
    locator: tuple[str, ...]
    char_start: int
    char_end: int
    body: str
    search_text: str


class KnowledgeIndex:
    """冻结语料的 BM25 索引, 带版本生效期过滤。"""

    def __init__(self) -> None:
        self._chunks: list[_IndexedChunk] = []
        self._term_frequency: list[Counter[str]] = []
        self._document_frequency: Counter[str] = Counter()
        self._lengths: list[int] = []
        self._validity: dict[str, tuple[date, date | None]] = {}

    def __len__(self) -> int:
        return len(self._chunks)

    def add_chunk(
        self,
        chunk: _IndexedChunk,
        *,
        effective_from: date,
        effective_until: date | None,
    ) -> None:
        """加入一个检索块, 并登记其版本的生效区间。"""

        tokens = _tokenize(chunk.search_text)
        counts = Counter(tokens)
        self._chunks.append(chunk)
        self._term_frequency.append(counts)
        self._lengths.append(len(tokens))
        for term in counts:
            self._document_frequency[term] += 1
        self._validity[chunk.edition_key] = (effective_from, effective_until)

    def is_effective(self, edition_key: str, as_of: date) -> bool:
        """按项目既有闭区间语义判断版本在参考日期是否生效。"""

        window = self._validity.get(edition_key)
        if window is None:
            return False
        effective_from, effective_until = window
        return effective_from <= as_of and (effective_until is None or as_of <= effective_until)

    def search(self, query: str, *, as_of: date, limit: int = 4) -> RetrievalHit:
        """检索并只返回参考日期下生效的版本片段。"""

        if not query.strip():
            raise RetrievalError("检索查询不能为空")
        if limit < 1:
            raise RetrievalError("检索数量上限必须为正数")

        total = len(self._chunks)
        if total == 0:
            raise RetrievalError("索引为空, 无法检索")

        average_length = sum(self._lengths) / total
        k1, b = 1.5, 0.75
        query_terms = Counter(_tokenize(query))

        scored: list[tuple[float, int]] = []
        for position in range(total):
            # 版本过滤前置: 失效版本不参与打分, 杜绝旧版制度被检索出来
            if not self.is_effective(self._chunks[position].edition_key, as_of):
                continue
            score = 0.0
            for term, query_frequency in query_terms.items():
                frequency = self._term_frequency[position][term]
                if frequency == 0:
                    continue
                document_frequency = self._document_frequency[term]
                inverse_frequency = math.log(
                    1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                length_ratio = self._lengths[position] / average_length
                denominator = frequency + k1 * (1 - b + b * length_ratio)
                score += inverse_frequency * frequency * (k1 + 1) / denominator * query_frequency
            if score > 0:
                scored.append((score, position))

        scored.sort(key=lambda pair: (-pair[0], self._chunks[pair[1]].chunk_key))

        citations: list[Citation] = []
        for score, position in scored[:limit]:
            chunk = self._chunks[position]
            citations.append(
                Citation(
                    edition_key=chunk.edition_key,
                    locator=chunk.locator,
                    node_key=chunk.node_key,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    body=chunk.body,
                    score=round(score, 4),
                )
            )

        effective_keys = sorted(
            {
                chunk.edition_key
                for chunk in self._chunks
                if self.is_effective(chunk.edition_key, as_of)
            }
        )
        filtered_keys = sorted(set(self._validity) - set(effective_keys))
        return RetrievalHit(
            query=query,
            as_of=as_of,
            citations=citations,
            effective_edition_keys=effective_keys,
            filtered_edition_keys=filtered_keys,
        )


def _locator_for(parsed: EditionParseResult, node_key: str) -> tuple[str, ...]:
    """从节点表里取回检索块所属节点的定位符。"""

    for node in parsed.nodes:
        if node.node_key == node_key:
            return tuple(node.locator)
    raise RetrievalError(f"检索块引用了不存在的节点: {node_key}")


def load_knowledge_index(knowledge_dir: str | Path) -> KnowledgeIndex:
    """从冻结语料构建检索索引, 生效期取自 corpus_v1.json。

    正文哈希在 `EditionInput` 构造时复算, 因此索引必然绑定当前冻结字节;
    清单与正文不一致会直接失败, 而不是检索到漂移文本。
    """

    knowledge_path = Path(knowledge_dir)
    manifest_path = knowledge_path / "manifests" / "corpus_v1.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalError(f"无法读取语料清单: {manifest_path}") from exc

    parsed_by_edition: dict[str, EditionParseResult] = parse_frozen_corpus(knowledge_path)
    index = KnowledgeIndex()
    for entry in manifest["entries"]:
        edition_key = str(entry["edition_key"])
        parsed = parsed_by_edition.get(edition_key)
        if parsed is None:
            raise RetrievalError(f"清单版本未解析出节点: {edition_key}")
        effective_from = date.fromisoformat(str(entry["effective_from"]))
        raw_until = entry.get("effective_until")
        effective_until = date.fromisoformat(str(raw_until)) if raw_until else None
        for chunk in parsed.chunks:
            index.add_chunk(
                _IndexedChunk(
                    chunk_key=chunk.chunk_key,
                    edition_key=edition_key,
                    node_key=chunk.node_key,
                    locator=_locator_for(parsed, chunk.node_key),
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    body=chunk.display_text,
                    search_text=chunk.search_text,
                ),
                effective_from=effective_from,
                effective_until=effective_until,
            )
    return index


__all__ = [
    "Citation",
    "KnowledgeIndex",
    "RetrievalError",
    "RetrievalHit",
    "load_knowledge_index",
]
