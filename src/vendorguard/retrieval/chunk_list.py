"""从现行内部制度生成, 落盘并加载扁平检索片段清单.

设计要点 (对应 M3 方案 6 的数据契约与 9 的 M3-1):

- 清单是代码从冻结语料确定性算出的派生物, 不是新的权威数据: 构建时读取
  normalized/manifest.json 与 manifests/corpus_v1.json, 核对快照与正文哈希后
  调用 parse_policy_edition(), 因此每条片段都带得回归一化正文的区间与哈希.
- 候选范围由程序侧的显式白名单限定: 被替代的版本与外部法规留在仓库里作负例,
  本期不为它们生成片段或 embedding, 模型与提示词都无法扩大这个范围.
- 片段只服务召回, 最终引用回指 node_key 对应的 KnowledgeNode: 记录里同时保留
  chunk_key 与 node_key, 并带上定位路径供报告核对原文.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from vendorguard.evidence.node_parser import EditionInput, parse_policy_edition
from vendorguard.evidence.schema import KnowledgeNode, RetrievalChunk

# 现行制度白名单: 本期检索与向量缓存的唯一候选范围 (M3 方案 3.2).
# 顺序即清单顺序, 固定下来才能让重复构建产出同一份 JSONL.
CURRENT_EDITION_KEYS: tuple[str, ...] = (
    "demo_supplier_admission_policy_v2",
    "demo_supplier_required_documents_policy_v3",
)

_NORMALIZED_MANIFEST = Path("normalized") / "manifest.json"
_CORPUS_MANIFEST = Path("manifests") / "corpus_v1.json"


class ChunkListError(ValueError):
    """片段清单缺失, 损坏或与冻结语料不一致时抛出的错误.

    调用方必须能区分"清单没有"和"检索没有依据": 前者是配置失败, 要直接中断,
    不能被当成"查不到相关制度"回给模型.
    """


class ChunkRecord(BaseModel):
    """一条可落盘的检索片段, 同时携带召回键与最终引用要用的节点键.

    display_text 与 char_start/char_end 沿用解析器的节点区间, 让片段被命中后总能
    回到归一化正文的逐字原文; search_text 额外携带标题与表头上下文, 只服务召回.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    document_title: str = Field(min_length=1)
    locator_path: tuple[str, ...] = Field(min_length=1)
    display_text: str = Field(min_length=1)
    search_text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    source_path: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("display_text", "search_text")
    @classmethod
    def validate_nonblank_text(cls, value: str) -> str:
        """展示文本与检索文本均不得只包含空白字符."""

        if not value.strip():
            raise ValueError("文本不能为空")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> ChunkRecord:
        """校验片段能回指原文, 与 RetrievalChunk 同一口径.

        检索层拿到这份清单后不再回查解析器, 所以"命中片段必能回到逐字原文"
        这条契约要随记录一起落盘, 而不是只活在解析结果里.
        """

        if self.char_end <= self.char_start:
            raise ValueError("字符区间终点必须晚于起点")

        if self.char_end - self.char_start != len(self.display_text):
            raise ValueError("字符区间长度必须等于展示原文长度")

        if self.display_text not in self.search_text:
            raise ValueError("检索文本必须包含展示原文")

        return self


def build_chunk_records(knowledge_dir: str | Path) -> tuple[ChunkRecord, ...]:
    """按现行白名单从冻结语料构建片段清单.

    只读两份语料清单与归一化正文, 不解析负例版本, 也不生成 embedding. 任一版本的
    快照字节或正文哈希与清单不符即失败: 片段必须追得到冻结的那份原始材料, 否则
    引用会指向一份无法回放的正文.
    """

    directory = Path(knowledge_dir)
    normalized_entries = _load_manifest_entries(directory / _NORMALIZED_MANIFEST)
    corpus_entries = _load_manifest_entries(directory / _CORPUS_MANIFEST)

    records: list[ChunkRecord] = []
    for edition_key in CURRENT_EDITION_KEYS:
        normalized_entry = normalized_entries.get(edition_key)
        corpus_entry = corpus_entries.get(edition_key)
        if normalized_entry is None or corpus_entry is None:
            raise ChunkListError(f"现行制度 {edition_key} 不在冻结语料清单中")

        snapshot_sha256 = str(corpus_entry["snapshot_sha256"])
        if str(normalized_entry["snapshot_sha256"]) != snapshot_sha256:
            raise ChunkListError(f"版本 {edition_key} 两份清单的快照哈希不一致")

        snapshot_path = directory / str(corpus_entry["snapshot_path"])
        try:
            snapshot_bytes = snapshot_path.read_bytes()
        except OSError as exc:
            raise ChunkListError(f"版本 {edition_key} 的冻结快照无法读取: {snapshot_path}") from exc
        if sha256(snapshot_bytes).hexdigest() != snapshot_sha256:
            raise ChunkListError(f"版本 {edition_key} 的快照与清单哈希不一致: {snapshot_path}")

        source_path = str(normalized_entry["normalized_path"])
        try:
            text = (directory / source_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ChunkListError(f"版本 {edition_key} 的归一化正文无法读取: {source_path}") from exc
        try:
            edition = EditionInput(
                edition_key=edition_key,
                document_title=str(corpus_entry["title"]),
                snapshot_sha256=snapshot_sha256,
                normalized_sha256=str(normalized_entry["normalized_sha256"]),
                text=text,
            )
        except ValidationError as exc:
            raise ChunkListError(f"版本 {edition_key} 的归一化正文与清单哈希不一致") from exc

        parsed = parse_policy_edition(edition)
        nodes = {node.node_key: node for node in parsed.nodes}
        for chunk in parsed.chunks:
            records.append(
                _build_record(
                    chunk,
                    _node_for_chunk(nodes, chunk),
                    document_title=edition.document_title,
                    source_path=source_path,
                )
            )
    return tuple(records)


def write_chunk_list(path: str | Path, records: Iterable[ChunkRecord]) -> None:
    """把片段清单写成一行一条的 UTF-8 JSONL, 不写 BOM, 换行固定为 LF.

    换行固定是刻意的: 清单要入库并在不同平台上重建, 不能让 Windows 的默认换行
    把整份文件改写成 CRLF 差异.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record.model_dump(mode="json"), ensure_ascii=False) for record in records]
    target.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")


def load_chunk_list(path: str | Path) -> tuple[ChunkRecord, ...]:
    """读取并校验片段清单, 缺失, 为空或含坏行时抛 ChunkListError.

    刻意不做"跳过坏行继续"的容错: 少一条片段会让检索静默失去依据, 而模型看到的
    结果与"制度真的没有规定"无法区分, 所以宁可整体失败. 这里同时复核每条记录的
    版本都在现行白名单内, 手改过的清单也塞不进负例版本.
    """

    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ChunkListError(f"片段清单无法读取: {source}") from exc

    records: list[ChunkRecord] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise ChunkListError(f"片段清单第 {number} 行为空: {source}")
        try:
            records.append(ChunkRecord.model_validate_json(line))
        except ValidationError as exc:
            raise ChunkListError(f"片段清单第 {number} 行未通过校验: {source}") from exc

    if not records:
        raise ChunkListError(f"片段清单为空: {source}")

    current = set(CURRENT_EDITION_KEYS)
    for record in records:
        if record.edition_key not in current:
            raise ChunkListError(f"片段清单含非现行版本 {record.edition_key}: {source}")

    return tuple(records)


def _load_manifest_entries(path: Path) -> dict[str, dict[str, Any]]:
    """读取一份语料清单并按 edition_key 建索引, 结构不符即抛 ChunkListError."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChunkListError(f"语料清单无法读取: {path}") from exc

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ChunkListError(f"语料清单缺少 entries 列表: {path}")

    indexed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "edition_key" not in entry:
            raise ChunkListError(f"语料清单条目缺少 edition_key: {path}")
        indexed[str(entry["edition_key"])] = entry
    return indexed


def _node_for_chunk(
    nodes: dict[str, KnowledgeNode],
    chunk: RetrievalChunk,
) -> KnowledgeNode:
    """取出检索块回指的节点, 并现场复核区间与正文一致."""

    node = nodes.get(chunk.node_key)
    if node is None:
        raise ChunkListError(f"检索块 {chunk.chunk_key} 引用了不存在的节点 {chunk.node_key}")

    same_span = (chunk.char_start, chunk.char_end) == (node.char_start, node.char_end)
    if chunk.display_text != node.body or not same_span:
        raise ChunkListError(f"检索块 {chunk.chunk_key} 与节点 {node.node_key} 的原文区间不一致")

    return node


def _build_record(
    chunk: RetrievalChunk,
    node: KnowledgeNode,
    *,
    document_title: str,
    source_path: str,
) -> ChunkRecord:
    """把解析器的检索块与它回指的节点合并成一条可落盘记录."""

    return ChunkRecord(
        chunk_key=chunk.chunk_key,
        node_key=node.node_key,
        edition_key=chunk.edition_key,
        document_title=document_title,
        locator_path=node.locator,
        display_text=chunk.display_text,
        search_text=chunk.search_text,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        source_path=source_path,
        snapshot_sha256=chunk.snapshot_sha256,
        normalized_sha256=chunk.normalized_sha256,
    )


__all__ = [
    "CURRENT_EDITION_KEYS",
    "ChunkListError",
    "ChunkRecord",
    "build_chunk_records",
    "load_chunk_list",
    "write_chunk_list",
]
