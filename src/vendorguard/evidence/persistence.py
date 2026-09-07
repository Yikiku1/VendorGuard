"""保存版本化知识语料和检索投影的不可变持久化模型。"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.database import Base

_SHA256_LENGTH = 64
_EMBEDDING_DIMENSIONS = 1024


class KnowledgeSourceOrigin(StrEnum):
    """知识来源的取得方式, 与规范语料清单中的取值一致。"""

    SYNTHETIC = "synthetic"
    GOV_DOCUMENT = "gov_document"
    COMMERCIAL = "commercial"


class KnowledgeAuthority(StrEnum):
    """知识文本的效力层级, 不表示其是否可作为直接准入依据。"""

    LAW = "law"
    ADMINISTRATIVE_REGULATION = "administrative_regulation"
    DEPARTMENT_RULE = "department_rule"
    PLATFORM_RULE = "platform_rule"
    INTERNAL = "internal"


class KnowledgeRole(StrEnum):
    """检索结果在供应商准入链路中允许承担的角色。"""

    DIRECT_POLICY = "direct_policy"
    BACKGROUND = "background"
    CONDITIONAL_REFERENCE = "conditional_reference"
    VERSION_NEGATIVE = "version_negative"
    BACKGROUND_AND_VERSION_NEGATIVE = "background_and_version_negative"


class KnowledgeNodeType(StrEnum):
    """规范正文可被最终引用的结构节点类型。"""

    DOCUMENT = "document"
    CHAPTER = "chapter"
    SECTION = "section"
    ARTICLE = "article"
    PARAGRAPH = "paragraph"
    ITEM = "item"
    TABLE = "table"
    TABLE_ROW = "table_row"


_SOURCE_ORIGIN_VALUES_SQL = ", ".join(f"'{value.value}'" for value in KnowledgeSourceOrigin)
_AUTHORITY_VALUES_SQL = ", ".join(f"'{value.value}'" for value in KnowledgeAuthority)
_ROLE_VALUES_SQL = ", ".join(f"'{value.value}'" for value in KnowledgeRole)
_NODE_TYPE_VALUES_SQL = ", ".join(f"'{value.value}'" for value in KnowledgeNodeType)


def _enum_column(enum_type: type[StrEnum], name: str) -> Enum:
    """创建受数据库 CHECK 约束的字符串枚举列类型。"""

    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda values: [value.value for value in values],
    )


class KnowledgeSourceRecord(Base):
    """保存来源稳定身份, 不承载某个版本的原始快照或正文。"""

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        CheckConstraint(
            f"source_origin IN ({_SOURCE_ORIGIN_VALUES_SQL})",
            name="knowledge_source_origin",
        ),
        CheckConstraint(
            "(source_origin = 'synthetic' AND canonical_url IS NULL AND retrieved_at IS NULL) "
            "OR (source_origin <> 'synthetic' AND canonical_url IS NOT NULL "
            "AND retrieved_at IS NOT NULL)",
            name="knowledge_source_provenance",
        ),
    )

    source_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    publisher: Mapped[str] = mapped_column(String(500), nullable=False)
    source_origin: Mapped[KnowledgeSourceOrigin] = mapped_column(
        _enum_column(KnowledgeSourceOrigin, "knowledge_source_origin"),
        nullable=False,
    )
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class KnowledgeEditionRecord(Base):
    """保存一份不可变知识版本与其确切快照出处、角色和有效期。"""

    __tablename__ = "knowledge_editions"
    __table_args__ = (
        CheckConstraint(
            f"authority IN ({_AUTHORITY_VALUES_SQL})",
            name="knowledge_edition_authority",
        ),
        CheckConstraint(f"role IN ({_ROLE_VALUES_SQL})", name="knowledge_edition_role"),
        CheckConstraint(
            "(canonical_url IS NULL AND retrieved_at IS NULL) OR "
            "(canonical_url IS NOT NULL AND retrieved_at IS NOT NULL)",
            name="knowledge_edition_provenance",
        ),
        CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from",
            name="knowledge_edition_effective_period",
        ),
        CheckConstraint(
            f"char_length(snapshot_sha256) = {_SHA256_LENGTH}",
            name="knowledge_edition_snapshot_sha256_length",
        ),
        CheckConstraint(
            f"char_length(normalized_sha256) = {_SHA256_LENGTH}",
            name="knowledge_edition_normalized_sha256_length",
        ),
    )

    edition_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    source_key: Mapped[str] = mapped_column(
        ForeignKey("knowledge_sources.source_key", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[str] = mapped_column(String(200), nullable=False)
    authority: Mapped[KnowledgeAuthority] = mapped_column(
        _enum_column(KnowledgeAuthority, "knowledge_authority"),
        nullable=False,
    )
    role: Mapped[KnowledgeRole] = mapped_column(
        _enum_column(KnowledgeRole, "knowledge_role"),
        nullable=False,
    )
    applicability: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_on: Mapped[date] = mapped_column(Date, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    snapshot_sha256: Mapped[str] = mapped_column(
        String(_SHA256_LENGTH),
        unique=True,
        nullable=False,
    )
    normalized_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    normalized_processor_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class KnowledgeEditionSupersession(Base):
    """保存版本替代关系, 支持一个新版显式替代多个前身。"""

    __tablename__ = "knowledge_edition_supersessions"
    __table_args__ = (
        CheckConstraint(
            "successor_edition_key <> predecessor_edition_key",
            name="knowledge_edition_supersession_not_self",
        ),
    )

    successor_edition_key: Mapped[str] = mapped_column(
        ForeignKey("knowledge_editions.edition_key", ondelete="RESTRICT"),
        primary_key=True,
    )
    predecessor_edition_key: Mapped[str] = mapped_column(
        ForeignKey("knowledge_editions.edition_key", ondelete="RESTRICT"),
        primary_key=True,
    )


class KnowledgeNodeRecord(Base):
    """保存可精确回放的最终引用节点, 不保存可变的检索扩展文本。"""

    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_node_key"],
            ["knowledge_nodes.node_key"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("char_end > char_start", name="knowledge_node_nonempty_span"),
        CheckConstraint(
            f"node_type IN ({_NODE_TYPE_VALUES_SQL})",
            name="knowledge_node_type",
        ),
        CheckConstraint(
            "char_length(body) = char_end - char_start",
            name="knowledge_node_body_length",
        ),
        CheckConstraint(
            f"char_length(snapshot_sha256) = {_SHA256_LENGTH}",
            name="knowledge_node_snapshot_sha256_length",
        ),
        CheckConstraint(
            f"char_length(normalized_sha256) = {_SHA256_LENGTH}",
            name="knowledge_node_normalized_sha256_length",
        ),
        CheckConstraint(
            f"char_length(body_sha256) = {_SHA256_LENGTH}",
            name="knowledge_node_body_sha256_length",
        ),
    )

    node_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    edition_key: Mapped[str] = mapped_column(
        ForeignKey("knowledge_editions.edition_key", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    parent_node_key: Mapped[str | None] = mapped_column(String(240), nullable=True)
    node_type: Mapped[KnowledgeNodeType] = mapped_column(
        _enum_column(KnowledgeNodeType, "knowledge_node_type"),
        nullable=False,
    )
    locator: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class RetrievalChunkRecord(Base):
    """保存节点的检索投影和可选向量, 不能代替 KnowledgeNode 最终引用。"""

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        CheckConstraint("char_end > char_start", name="retrieval_chunk_nonempty_span"),
        CheckConstraint(
            "char_length(display_text) = char_end - char_start",
            name="retrieval_chunk_display_text_length",
        ),
        CheckConstraint(
            "position(display_text in search_text) > 0",
            name="retrieval_chunk_search_includes_display",
        ),
        CheckConstraint(
            f"char_length(snapshot_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_snapshot_sha256_length",
        ),
        CheckConstraint(
            f"char_length(normalized_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_normalized_sha256_length",
        ),
        CheckConstraint(
            f"char_length(node_body_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_node_body_sha256_length",
        ),
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL AND embedding_dimensions IS NULL "
            "AND embedding_content_sha256 IS NULL) OR (embedding IS NOT NULL "
            f"AND embedding_model IS NOT NULL AND embedding_dimensions = {_EMBEDDING_DIMENSIONS} "
            "AND embedding_content_sha256 IS NOT NULL)",
            name="retrieval_chunk_embedding_metadata",
        ),
        CheckConstraint(
            "embedding_content_sha256 IS NULL OR "
            f"char_length(embedding_content_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_embedding_content_sha256_length",
        ),
    )

    chunk_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    edition_key: Mapped[str] = mapped_column(
        ForeignKey("knowledge_editions.edition_key", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    node_key: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.node_key", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    snapshot_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    node_body_sha256: Mapped[str] = mapped_column(String(_SHA256_LENGTH), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_EMBEDDING_DIMENSIONS),
        nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_content_sha256: Mapped[str | None] = mapped_column(
        String(_SHA256_LENGTH),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


__all__ = [
    "KnowledgeAuthority",
    "KnowledgeEditionRecord",
    "KnowledgeEditionSupersession",
    "KnowledgeNodeRecord",
    "KnowledgeNodeType",
    "KnowledgeRole",
    "KnowledgeSourceOrigin",
    "KnowledgeSourceRecord",
    "RetrievalChunkRecord",
]
