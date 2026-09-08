"""RAG 语料四层持久化模型: 来源, 版本快照, 结构节点与检索块.

表布局对应 evidence.schema 的不可变语料模型与 node_parser 的解析产物,
与 PRD 里 rag_documents/rag_chunks 的两表表述的差异已在重构待办定案:
来源与版本拆开后, 生效期硬过滤, 版本血缘和快照哈希绑定才有落点.

设计纪律 (全部同时兑现到数据库层, 不依赖应用代码自觉):
- 四张表只增不改不删: 历史引用一旦入库即冻结, 重新入库走幂等跳过,
  重切块产生新键记录, 不覆盖旧记录.
- 哈希, 字符区间与原文的一致性用 CHECK 约束在库内可验: body 长度必须
  等于区间长度, search_text 必须包含 display_text, sha256 定长 64.
- 定位符在版本内唯一由 UNIQUE (edition_id, locator) 兜底, 这正是冻结
  评测集 node.locator == target.locator 精确匹配语义的库内镜像.
- embedding 三件套 (模型名, 维度, 被嵌入文本哈希) 全空或全有, 维度钉死
  1024, 模型换代或混库时拒绝半套状态.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from vendorguard.database import Base

# 与探针实测一致: qwen3.7-text-embedding 输出 1024 维且已 L2 归一.
EMBEDDING_DIMENSIONS = 1024


class RagSourceOrigin(StrEnum):
    """语料来源性质, 决定出处双向校验与版权口径"""

    SYNTHETIC = "synthetic"
    GOV_DOCUMENT = "gov_document"
    COMMERCIAL = "commercial"


class RagEditionRole(StrEnum):
    """版本在一期证据链中的角色, 检索前硬过滤的依据之一"""

    DIRECT_POLICY = "direct_policy"
    BACKGROUND = "background"
    CONDITIONAL_REFERENCE = "conditional_reference"
    VERSION_NEGATIVE = "version_negative"
    BACKGROUND_AND_VERSION_NEGATIVE = "background_and_version_negative"


class RagNodeType(StrEnum):
    """结构节点类型, 与 evidence.schema.NodeType 受控集合逐字一致"""

    DOCUMENT = "document"
    CHAPTER = "chapter"
    SECTION = "section"
    ARTICLE = "article"
    PARAGRAPH = "paragraph"
    ITEM = "item"
    TABLE = "table"
    TABLE_ROW = "table_row"


def _values_sql(enum_type: type[StrEnum]) -> str:
    """生成 CHECK 约束用的 SQL 值清单."""

    return ", ".join(f"'{item.value}'" for item in enum_type)


def _sha_provenance_checks() -> tuple[CheckConstraint, ...]:
    """返回快照与归一化双哈希的定长 CHECK 约束组.

    写成函数而非模块常量: 常量列表在两处 table_args 解包会共享同一
    CheckConstraint 对象实例, Alembic 与 SQLAlchemy 都要求每表独立对象.
    必须在类定义之前声明, 因为 table_args 在模块导入时就求值.
    """

    return tuple(
        CheckConstraint(sql, name=name)
        for sql, name in (
            ("char_length(snapshot_sha256) = 64", "sha_snapshot_length"),
            ("char_length(normalized_sha256) = 64", "sha_normalized_length"),
        )
    )


class RagSource(Base):
    """保存知识来源的稳定身份, 不承载版本正文."""

    __tablename__ = "rag_sources"
    __table_args__ = (
        CheckConstraint(
            f"source_origin IN ({_values_sql(RagSourceOrigin)})",
            name="rag_source_origin",
        ),
        CheckConstraint(
            "(source_origin = 'synthetic' AND canonical_url IS NULL "
            "AND retrieved_at IS NULL) OR "
            "(source_origin <> 'synthetic' AND canonical_url IS NOT NULL "
            "AND retrieved_at IS NOT NULL)",
            name="rag_source_provenance",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    publisher: Mapped[str] = mapped_column(String(200), nullable=False)
    source_origin: Mapped[RagSourceOrigin] = mapped_column(
        Enum(
            RagSourceOrigin,
            name="rag_source_origin",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    canonical_url: Mapped[str | None] = mapped_column(String(500))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class RagEdition(Base):
    """保存不可变版本快照的元数据, 是生效期过滤与血缘回放的锚点."""

    __tablename__ = "rag_editions"
    __table_args__ = (
        CheckConstraint(
            f"role IN ({_values_sql(RagEditionRole)})",
            name="rag_edition_role",
        ),
        CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from",
            name="rag_edition_effective_range",
        ),
        CheckConstraint(
            "char_length(snapshot_sha256) = 64",
            name="rag_edition_snapshot_sha_length",
        ),
        CheckConstraint(
            "char_length(normalized_sha256) = 64",
            name="rag_edition_normalized_sha_length",
        ),
        UniqueConstraint("edition_key", name="uq_rag_editions_edition_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_sources.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    edition_key: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    published_on: Mapped[date] = mapped_column(Date, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_until: Mapped[date | None] = mapped_column(Date)
    role: Mapped[RagEditionRole] = mapped_column(
        Enum(
            RagEditionRole,
            name="rag_edition_role",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    applicability: Mapped[str] = mapped_column(String(500), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class RagEditionSupersedes(Base):
    """版本血缘边表: supersedes 是列表, 1:N 替代关系逐边成行.

    只能表达"谁替代谁"; "必须同源"这条约束依赖两行 edition 的 source_id
    比对, 数据库层用 CHECK 表达代价过高, 由清单模型与入库服务承担.
    """

    __tablename__ = "rag_edition_supersedes"
    __table_args__ = (
        CheckConstraint(
            "edition_id <> predecessor_edition_id",
            name="rag_supersedes_not_self",
        ),
    )

    edition_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_editions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    predecessor_edition_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_editions.id", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )


class RagNode(Base):
    """保存可回放的不可变结构节点, 定位符在版本内唯一.

    length(body) = char_end - char_start 是解析器逐字切片契约的库内镜像:
    任何绕过应用校验的直写都会被约束挡住, 历史引用因此不可能悄悄漂移.
    """

    __tablename__ = "rag_nodes"
    __table_args__ = (
        CheckConstraint(
            f"node_type IN ({_values_sql(RagNodeType)})",
            name="rag_node_type",
        ),
        CheckConstraint(
            "char_start >= 0 AND char_end > char_start",
            name="rag_node_char_range",
        ),
        CheckConstraint(
            "length(body) = char_end - char_start",
            name="rag_node_body_matches_span",
        ),
        CheckConstraint(
            "array_length(locator, 1) >= 1 AND array_position(locator, '') IS NULL",
            name="rag_node_locator_nonempty",
        ),
        CheckConstraint(
            "char_length(body_sha256) = 64",
            name="rag_node_body_sha_length",
        ),
        *_sha_provenance_checks(),
        UniqueConstraint("edition_id", "locator", name="uq_rag_nodes_edition_locator"),
        UniqueConstraint("node_key", name="uq_rag_nodes_node_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    edition_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_editions.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    parent_node_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rag_nodes.id", ondelete="RESTRICT"),
        index=True,
    )
    node_key: Mapped[str] = mapped_column(String(200), nullable=False)
    node_type: Mapped[RagNodeType] = mapped_column(
        Enum(
            RagNodeType,
            name="rag_node_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        nullable=False,
    )
    locator: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class RagChunk(Base):
    """保存仅供召回的不可变检索投影, 引用一律回指 rag_nodes.

    embedding 三件套全空或全有 + 维度钉死 1024: 模型换代或混库时, 半套
    状态在约束层就被拒绝, 而不是到检索时才拿到跨代向量.
    """

    __tablename__ = "rag_chunks"
    __table_args__ = (
        CheckConstraint(
            "char_start >= 0 AND char_end > char_start",
            name="rag_chunk_char_range",
        ),
        CheckConstraint(
            "length(display_text) = char_end - char_start",
            name="rag_chunk_display_matches_span",
        ),
        CheckConstraint(
            "strpos(search_text, display_text) > 0",
            name="rag_chunk_search_contains_display",
        ),
        CheckConstraint(
            "char_length(node_body_sha256) = 64",
            name="rag_chunk_node_body_sha_length",
        ),
        *_sha_provenance_checks(),
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL AND embedding_dim IS NULL "
            "AND embedded_text_sha256 IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_model IS NOT NULL "
            "AND embedding_dim IS NOT NULL AND embedded_text_sha256 IS NOT NULL)",
            name="rag_chunk_embedding_all_or_none",
        ),
        CheckConstraint(
            f"embedding_dim IS NULL OR embedding_dim = {EMBEDDING_DIMENSIONS}",
            name="rag_chunk_embedding_dim_pinned",
        ),
        UniqueConstraint("chunk_key", name="uq_rag_chunks_chunk_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    edition_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_editions.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("rag_nodes.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    chunk_key: Mapped[str] = mapped_column(String(200), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    node_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    embedded_text_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
