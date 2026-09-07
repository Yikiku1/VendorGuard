"""校验冻结语料并以不可覆盖方式写入可审计知识库。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.evidence.node_parser import parse_knowledge_nodes
from vendorguard.evidence.persistence import (
    KnowledgeAuthority,
    KnowledgeEditionRecord,
    KnowledgeEditionSupersession,
    KnowledgeNodeRecord,
    KnowledgeNodeType,
    KnowledgeRole,
    KnowledgeSourceOrigin,
    KnowledgeSourceRecord,
    RetrievalChunkRecord,
)
from vendorguard.evidence.schema import KnowledgeNode, RetrievalChunk


class CorpusIngestionError(ValueError):
    """正式清单、冻结文件或归一化正文无法形成一致入库计划时抛出。"""


@dataclass(frozen=True)
class KnowledgeSourceInput:
    """由正式清单推导的来源稳定身份。"""

    source_key: str
    title: str
    publisher: str
    source_origin: KnowledgeSourceOrigin
    canonical_url: str | None
    retrieved_at: datetime | None


@dataclass(frozen=True)
class KnowledgeEditionInput:
    """一份冻结版本的入库事实, 包含该版本的精确公开出处。"""

    edition_key: str
    source_key: str
    title: str
    version: str
    authority: KnowledgeAuthority
    role: KnowledgeRole
    applicability: str
    canonical_url: str | None
    retrieved_at: datetime | None
    published_on: date
    effective_from: date
    effective_until: date | None
    snapshot_sha256: str
    normalized_sha256: str
    normalized_processor_version: str
    supersedes: tuple[str, ...]


@dataclass(frozen=True)
class CorpusIngestionPlan:
    """经文件和哈希校验后的全部追加写入内容, 不含数据库连接。"""

    sources: tuple[KnowledgeSourceInput, ...]
    editions: tuple[KnowledgeEditionInput, ...]
    nodes: tuple[KnowledgeNode, ...]
    chunks: tuple[RetrievalChunk, ...]


@dataclass(frozen=True)
class CorpusIngestionResult:
    """描述一次幂等写入请求覆盖的记录数量, 非本次实际新增行数。"""

    source_count: int
    edition_count: int
    supersession_count: int
    node_count: int
    chunk_count: int


def _sha256(content: bytes) -> str:
    """计算与正式清单一致的小写 SHA-256 摘要。"""

    return sha256(content).hexdigest()


def _read_json_object(path: Path) -> dict[str, object]:
    """读取顶层必须为 JSON 对象的正式清单。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusIngestionError(f"无法读取正式清单: {path}") from exc

    if not isinstance(payload, dict):
        raise CorpusIngestionError(f"正式清单顶层必须是对象: {path}")
    return payload


def _required_string(entry: Mapping[str, object], field: str, edition_key: str) -> str:
    """读取非空字符串字段, 错误信息固定带上所属版本键。"""

    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise CorpusIngestionError(f"版本 {edition_key} 缺少有效字段: {field}")
    return value


def _optional_string(entry: Mapping[str, object], field: str, edition_key: str) -> str | None:
    """读取可为空字符串字段, 拒绝数字等会破坏来源回放的类型。"""

    value = entry.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CorpusIngestionError(f"版本 {edition_key} 的字段类型无效: {field}")
    return value


def _required_entries(payload: Mapping[str, object], name: str) -> list[dict[str, object]]:
    """读取由对象组成的 entries 数组。"""

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise CorpusIngestionError(f"{name} 缺少 entries 数组")
    if not all(isinstance(entry, dict) for entry in entries):
        raise CorpusIngestionError(f"{name} 的 entries 必须全部为对象")
    return entries


def _parse_date(value: str, field: str, edition_key: str) -> date:
    """解析清单使用的 ISO 日期。"""

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CorpusIngestionError(f"版本 {edition_key} 的日期无效: {field}") from exc


def _parse_datetime(value: str | None, edition_key: str) -> datetime | None:
    """解析外部快照抓取时刻, 严格要求携带时区。"""

    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorpusIngestionError(f"版本 {edition_key} 的抓取时间无效") from exc
    if parsed.utcoffset() is None:
        raise CorpusIngestionError(f"版本 {edition_key} 的抓取时间必须包含时区")
    return parsed


def _resolve_knowledge_file(knowledge_root: Path, relative_path: str, edition_key: str) -> Path:
    """解析清单中的相对路径, 禁止越出 data/knowledge。"""

    root = knowledge_root.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise CorpusIngestionError(f"版本 {edition_key} 的语料路径越出知识目录")
    if not candidate.is_file():
        raise CorpusIngestionError(f"版本 {edition_key} 缺少语料文件: {relative_path}")
    return candidate


def _projection_for_node(node: KnowledgeNode) -> RetrievalChunk:
    """为一个最终引用节点生成单一检索投影, 不改写节点正文。"""

    context = " ".join(node.locator)
    search_text = f"{context}\n{node.body}" if context else node.body
    return RetrievalChunk(
        chunk_key=f"{node.node_key}_chunk_0001",
        edition_key=node.edition_key,
        node_key=node.node_key,
        snapshot_sha256=node.snapshot_sha256,
        normalized_sha256=node.normalized_sha256,
        node_body_sha256=node.body_sha256,
        char_start=node.char_start,
        char_end=node.char_end,
        display_text=node.body,
        search_text=search_text,
    )


def load_frozen_corpus_plan(
    *,
    corpus_manifest_path: str | Path,
    normalized_manifest_path: str | Path,
) -> CorpusIngestionPlan:
    """从两份正式清单和冻结文件构造可入库计划, 不执行任何数据库写入。"""

    corpus_path = Path(corpus_manifest_path)
    normalized_path = Path(normalized_manifest_path)
    knowledge_root = corpus_path.parent.parent
    corpus_entries = _required_entries(_read_json_object(corpus_path), "正式语料清单")
    normalized_payload = _read_json_object(normalized_path)
    normalized_entries = _required_entries(normalized_payload, "归一化清单")
    processor_version = _required_string(normalized_payload, "processor_version", "归一化清单")

    normalized_by_edition: dict[str, dict[str, object]] = {}
    for normalized_entry in normalized_entries:
        edition_key = _required_string(normalized_entry, "edition_key", "归一化清单")
        if edition_key in normalized_by_edition:
            raise CorpusIngestionError(f"归一化清单中的版本重复: {edition_key}")
        normalized_by_edition[edition_key] = normalized_entry

    source_by_key: dict[str, KnowledgeSourceInput] = {}
    edition_by_key: dict[str, KnowledgeEditionInput] = {}
    nodes: list[KnowledgeNode] = []
    chunks: list[RetrievalChunk] = []

    for entry in corpus_entries:
        edition_key = _required_string(entry, "edition_key", "正式语料清单")
        if edition_key in edition_by_key:
            raise CorpusIngestionError(f"正式语料清单中的版本重复: {edition_key}")

        if edition_key not in normalized_by_edition:
            raise CorpusIngestionError(f"版本 {edition_key} 缺少归一化正文登记")
        normalized_entry = normalized_by_edition.pop(edition_key)

        snapshot_sha256 = _required_string(entry, "snapshot_sha256", edition_key)
        snapshot_path = _resolve_knowledge_file(
            knowledge_root,
            _required_string(entry, "snapshot_path", edition_key),
            edition_key,
        )
        if _sha256(snapshot_path.read_bytes()) != snapshot_sha256:
            raise CorpusIngestionError(f"版本 {edition_key} 的原始快照哈希不匹配")

        normalized_sha256 = _required_string(normalized_entry, "normalized_sha256", edition_key)
        normalized_snapshot_sha256 = _required_string(
            normalized_entry,
            "snapshot_sha256",
            edition_key,
        )
        if normalized_snapshot_sha256 != snapshot_sha256:
            raise CorpusIngestionError(f"版本 {edition_key} 的归一化输入快照不匹配")
        normalized_file = _resolve_knowledge_file(
            knowledge_root,
            _required_string(normalized_entry, "normalized_path", edition_key),
            edition_key,
        )
        text = normalized_file.read_text(encoding="utf-8")
        if _sha256(text.encode("utf-8")) != normalized_sha256:
            raise CorpusIngestionError(f"版本 {edition_key} 的归一化正文哈希不匹配")

        source_key = _required_string(entry, "source_key", edition_key)
        source_origin = KnowledgeSourceOrigin(_required_string(entry, "source_origin", edition_key))
        canonical_url = _optional_string(entry, "canonical_url", edition_key)
        retrieved_at = _parse_datetime(
            _optional_string(entry, "retrieved_at", edition_key),
            edition_key,
        )
        if (canonical_url is None) != (retrieved_at is None):
            raise CorpusIngestionError(
                f"版本 {edition_key} 的来源地址与抓取时间必须同时存在或同时为空"
            )

        source = KnowledgeSourceInput(
            source_key=source_key,
            title=_required_string(entry, "publisher", edition_key),
            publisher=_required_string(entry, "publisher", edition_key),
            source_origin=source_origin,
            canonical_url=canonical_url,
            retrieved_at=retrieved_at,
        )
        previous_source = source_by_key.get(source_key)
        if previous_source is None:
            source_by_key[source_key] = source
        elif (
            previous_source.title != source.title
            or previous_source.publisher != source.publisher
            or previous_source.source_origin != source.source_origin
        ):
            raise CorpusIngestionError(f"来源 {source_key} 在正式清单中的稳定身份不一致")

        supersedes_raw = entry.get("supersedes")
        if not isinstance(supersedes_raw, list) or not all(
            isinstance(predecessor, str) and predecessor for predecessor in supersedes_raw
        ):
            raise CorpusIngestionError(f"版本 {edition_key} 的 supersedes 必须是字符串数组")
        supersedes = tuple(supersedes_raw)
        if len(supersedes) != len(set(supersedes)) or edition_key in supersedes:
            raise CorpusIngestionError(f"版本 {edition_key} 的 supersedes 无效")

        edition = KnowledgeEditionInput(
            edition_key=edition_key,
            source_key=source_key,
            title=_required_string(entry, "title", edition_key),
            version=_required_string(entry, "version", edition_key),
            authority=KnowledgeAuthority(_required_string(entry, "authority", edition_key)),
            role=KnowledgeRole(_required_string(entry, "role", edition_key)),
            applicability=_required_string(entry, "applicability", edition_key),
            canonical_url=canonical_url,
            retrieved_at=retrieved_at,
            published_on=_parse_date(
                _required_string(entry, "published_on", edition_key),
                "published_on",
                edition_key,
            ),
            effective_from=_parse_date(
                _required_string(entry, "effective_from", edition_key),
                "effective_from",
                edition_key,
            ),
            effective_until=(
                _parse_date(effective_until, "effective_until", edition_key)
                if (effective_until := _optional_string(entry, "effective_until", edition_key))
                is not None
                else None
            ),
            snapshot_sha256=snapshot_sha256,
            normalized_sha256=normalized_sha256,
            normalized_processor_version=_required_string(
                normalized_entry,
                "processor_version",
                edition_key,
            ),
            supersedes=supersedes,
        )
        if edition.normalized_processor_version != processor_version:
            raise CorpusIngestionError(f"版本 {edition_key} 的归一化处理器版本不一致")
        if edition.effective_until is not None and edition.effective_until < edition.effective_from:
            raise CorpusIngestionError(f"版本 {edition_key} 的生效期方向无效")

        edition_by_key[edition_key] = edition
        edition_nodes = parse_knowledge_nodes(
            edition_key=edition.edition_key,
            snapshot_sha256=edition.snapshot_sha256,
            normalized_sha256=edition.normalized_sha256,
            text=text,
        )
        if not edition_nodes:
            raise CorpusIngestionError(f"版本 {edition_key} 未生成可引用节点")
        nodes.extend(edition_nodes)
        chunks.extend(_projection_for_node(node) for node in edition_nodes)

    if normalized_by_edition:
        extras = ", ".join(sorted(normalized_by_edition))
        raise CorpusIngestionError(f"归一化清单包含未登记的版本: {extras}")

    for edition in edition_by_key.values():
        for predecessor_key in edition.supersedes:
            predecessor = edition_by_key.get(predecessor_key)
            if predecessor is None:
                raise CorpusIngestionError(
                    f"版本 {edition.edition_key} 替代了不存在的前身: {predecessor_key}"
                )
            if predecessor.source_key != edition.source_key:
                raise CorpusIngestionError(f"版本 {edition.edition_key} 不能替代不同来源的前身")

    return CorpusIngestionPlan(
        sources=tuple(source_by_key.values()),
        editions=tuple(edition_by_key.values()),
        nodes=tuple(nodes),
        chunks=tuple(chunks),
    )


async def _insert_immutable_rows(
    session: AsyncSession,
    table: type[
        KnowledgeSourceRecord
        | KnowledgeEditionRecord
        | KnowledgeEditionSupersession
        | KnowledgeNodeRecord
        | RetrievalChunkRecord
    ],
    rows: Sequence[Mapping[str, object]],
) -> None:
    """按主键冲突忽略写入不可变行, 从而支持同一冻结语料安全重试。"""

    for row in rows:
        statement = insert(table).values(dict(row)).on_conflict_do_nothing()
        await session.execute(statement)


async def ingest_frozen_corpus(
    session: AsyncSession,
    plan: CorpusIngestionPlan,
) -> CorpusIngestionResult:
    """按外键依赖顺序幂等追加已校验计划, 由调用方负责事务提交或回滚。"""

    await _insert_immutable_rows(
        session,
        KnowledgeSourceRecord,
        tuple(
            {
                "source_key": source.source_key,
                "title": source.title,
                "publisher": source.publisher,
                "source_origin": source.source_origin,
                "canonical_url": source.canonical_url,
                "retrieved_at": source.retrieved_at,
            }
            for source in plan.sources
        ),
    )
    await _insert_immutable_rows(
        session,
        KnowledgeEditionRecord,
        tuple(
            {
                "edition_key": edition.edition_key,
                "source_key": edition.source_key,
                "title": edition.title,
                "version": edition.version,
                "authority": edition.authority,
                "role": edition.role,
                "applicability": edition.applicability,
                "canonical_url": edition.canonical_url,
                "retrieved_at": edition.retrieved_at,
                "published_on": edition.published_on,
                "effective_from": edition.effective_from,
                "effective_until": edition.effective_until,
                "snapshot_sha256": edition.snapshot_sha256,
                "normalized_sha256": edition.normalized_sha256,
                "normalized_processor_version": edition.normalized_processor_version,
            }
            for edition in plan.editions
        ),
    )
    await _insert_immutable_rows(
        session,
        KnowledgeEditionSupersession,
        tuple(
            {
                "successor_edition_key": edition.edition_key,
                "predecessor_edition_key": predecessor_key,
            }
            for edition in plan.editions
            for predecessor_key in edition.supersedes
        ),
    )
    await _insert_immutable_rows(
        session,
        KnowledgeNodeRecord,
        tuple(
            {
                "node_key": node.node_key,
                "edition_key": node.edition_key,
                "parent_node_key": node.parent_node_key,
                "node_type": KnowledgeNodeType(node.node_type),
                "locator": list(node.locator),
                "snapshot_sha256": node.snapshot_sha256,
                "normalized_sha256": node.normalized_sha256,
                "char_start": node.char_start,
                "char_end": node.char_end,
                "body": node.body,
                "body_sha256": node.body_sha256,
            }
            for node in plan.nodes
        ),
    )
    await _insert_immutable_rows(
        session,
        RetrievalChunkRecord,
        tuple(
            {
                "chunk_key": chunk.chunk_key,
                "edition_key": chunk.edition_key,
                "node_key": chunk.node_key,
                "snapshot_sha256": chunk.snapshot_sha256,
                "normalized_sha256": chunk.normalized_sha256,
                "node_body_sha256": chunk.node_body_sha256,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "display_text": chunk.display_text,
                "search_text": chunk.search_text,
                "embedding": None,
                "embedding_model": None,
                "embedding_dimensions": None,
                "embedding_content_sha256": None,
            }
            for chunk in plan.chunks
        ),
    )

    return CorpusIngestionResult(
        source_count=len(plan.sources),
        edition_count=len(plan.editions),
        supersession_count=sum(len(edition.supersedes) for edition in plan.editions),
        node_count=len(plan.nodes),
        chunk_count=len(plan.chunks),
    )


__all__ = [
    "CorpusIngestionError",
    "CorpusIngestionPlan",
    "CorpusIngestionResult",
    "ingest_frozen_corpus",
    "load_frozen_corpus_plan",
]
