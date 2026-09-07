"""create auditable knowledge corpus tables

Revision ID: 2f31cceb123a
Revises: 8acab623700e
Create Date: 2026-09-07 13:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2f31cceb123a"
down_revision: str | Sequence[str] | None = "8acab623700e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256_LENGTH = 64
_EMBEDDING_DIMENSIONS = 1024
_SOURCE_ORIGINS = "'synthetic', 'gov_document', 'commercial'"
_AUTHORITIES = "'law', 'administrative_regulation', 'department_rule', 'platform_rule', 'internal'"
_ROLES = (
    "'direct_policy', 'background', 'conditional_reference', "
    "'version_negative', 'background_and_version_negative'"
)
_NODE_TYPES = (
    "'document', 'chapter', 'section', 'article', 'paragraph', 'item', 'table', 'table_row'"
)
_IMMUTABLE_TABLES = (
    "knowledge_sources",
    "knowledge_editions",
    "knowledge_edition_supersessions",
    "knowledge_nodes",
    "retrieval_chunks",
)


def upgrade() -> None:
    """创建可回放的版本化语料表, 并禁止已写入证据被修改或删除。"""

    op.create_table(
        "knowledge_sources",
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("publisher", sa.String(length=500), nullable=False),
        sa.Column(
            "source_origin",
            sa.Enum(
                "synthetic",
                "gov_document",
                "commercial",
                name="knowledge_source_origin",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"source_origin IN ({_SOURCE_ORIGINS})",
            name="knowledge_source_origin",
        ),
        sa.CheckConstraint(
            "(source_origin = 'synthetic' AND canonical_url IS NULL AND retrieved_at IS NULL) "
            "OR (source_origin <> 'synthetic' AND canonical_url IS NOT NULL "
            "AND retrieved_at IS NOT NULL)",
            name="knowledge_source_provenance",
        ),
        sa.PrimaryKeyConstraint("source_key"),
    )
    op.create_table(
        "knowledge_editions",
        sa.Column("edition_key", sa.String(length=200), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("version", sa.String(length=200), nullable=False),
        sa.Column(
            "authority",
            sa.Enum(
                "law",
                "administrative_regulation",
                "department_rule",
                "platform_rule",
                "internal",
                name="knowledge_authority",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(
                "direct_policy",
                "background",
                "conditional_reference",
                "version_negative",
                "background_and_version_negative",
                name="knowledge_role",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("applicability", sa.Text(), nullable=False),
        sa.Column("published_on", sa.Date(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_until", sa.Date(), nullable=True),
        sa.Column("snapshot_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("normalized_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("normalized_processor_version", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"authority IN ({_AUTHORITIES})",
            name="knowledge_edition_authority",
        ),
        sa.CheckConstraint(f"role IN ({_ROLES})", name="knowledge_edition_role"),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from",
            name="knowledge_edition_effective_period",
        ),
        sa.CheckConstraint(
            f"char_length(snapshot_sha256) = {_SHA256_LENGTH}",
            name="knowledge_edition_snapshot_sha256_length",
        ),
        sa.CheckConstraint(
            f"char_length(normalized_sha256) = {_SHA256_LENGTH}",
            name="knowledge_edition_normalized_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["source_key"],
            ["knowledge_sources.source_key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("edition_key"),
        sa.UniqueConstraint("snapshot_sha256"),
    )
    op.create_index("ix_knowledge_editions_source_key", "knowledge_editions", ["source_key"])
    op.create_table(
        "knowledge_edition_supersessions",
        sa.Column("successor_edition_key", sa.String(length=200), nullable=False),
        sa.Column("predecessor_edition_key", sa.String(length=200), nullable=False),
        sa.CheckConstraint(
            "successor_edition_key <> predecessor_edition_key",
            name="knowledge_edition_supersession_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_edition_key"],
            ["knowledge_editions.edition_key"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_edition_key"],
            ["knowledge_editions.edition_key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("successor_edition_key", "predecessor_edition_key"),
    )
    op.create_table(
        "knowledge_nodes",
        sa.Column("node_key", sa.String(length=240), nullable=False),
        sa.Column("edition_key", sa.String(length=200), nullable=False),
        sa.Column("parent_node_key", sa.String(length=240), nullable=True),
        sa.Column(
            "node_type",
            sa.Enum(
                "document",
                "chapter",
                "section",
                "article",
                "paragraph",
                "item",
                "table",
                "table_row",
                name="knowledge_node_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("normalized_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_end > char_start", name="knowledge_node_nonempty_span"),
        sa.CheckConstraint(
            f"node_type IN ({_NODE_TYPES})",
            name="knowledge_node_type",
        ),
        sa.CheckConstraint(
            "char_length(body) = char_end - char_start",
            name="knowledge_node_body_length",
        ),
        sa.CheckConstraint(
            f"char_length(snapshot_sha256) = {_SHA256_LENGTH}",
            name="knowledge_node_snapshot_sha256_length",
        ),
        sa.CheckConstraint(
            f"char_length(normalized_sha256) = {_SHA256_LENGTH}",
            name="knowledge_node_normalized_sha256_length",
        ),
        sa.CheckConstraint(
            f"char_length(body_sha256) = {_SHA256_LENGTH}",
            name="knowledge_node_body_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["edition_key"],
            ["knowledge_editions.edition_key"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_node_key"],
            ["knowledge_nodes.node_key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("node_key"),
    )
    op.create_index("ix_knowledge_nodes_edition_key", "knowledge_nodes", ["edition_key"])
    op.create_table(
        "retrieval_chunks",
        sa.Column("chunk_key", sa.String(length=240), nullable=False),
        sa.Column("edition_key", sa.String(length=200), nullable=False),
        sa.Column("node_key", sa.String(length=240), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("normalized_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("node_body_sha256", sa.String(length=_SHA256_LENGTH), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(_EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("embedding_content_sha256", sa.String(length=_SHA256_LENGTH), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_end > char_start", name="retrieval_chunk_nonempty_span"),
        sa.CheckConstraint(
            "char_length(display_text) = char_end - char_start",
            name="retrieval_chunk_display_text_length",
        ),
        sa.CheckConstraint(
            "position(display_text in search_text) > 0",
            name="retrieval_chunk_search_includes_display",
        ),
        sa.CheckConstraint(
            f"char_length(snapshot_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_snapshot_sha256_length",
        ),
        sa.CheckConstraint(
            f"char_length(normalized_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_normalized_sha256_length",
        ),
        sa.CheckConstraint(
            f"char_length(node_body_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_node_body_sha256_length",
        ),
        sa.CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL AND embedding_dimensions IS NULL "
            "AND embedding_content_sha256 IS NULL) OR (embedding IS NOT NULL "
            f"AND embedding_model IS NOT NULL AND embedding_dimensions = {_EMBEDDING_DIMENSIONS} "
            "AND embedding_content_sha256 IS NOT NULL)",
            name="retrieval_chunk_embedding_metadata",
        ),
        sa.CheckConstraint(
            "embedding_content_sha256 IS NULL OR "
            f"char_length(embedding_content_sha256) = {_SHA256_LENGTH}",
            name="retrieval_chunk_embedding_content_sha256_length",
        ),
        sa.ForeignKeyConstraint(
            ["edition_key"],
            ["knowledge_editions.edition_key"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["node_key"],
            ["knowledge_nodes.node_key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("chunk_key"),
    )
    op.create_index("ix_retrieval_chunks_edition_key", "retrieval_chunks", ["edition_key"])
    op.create_index("ix_retrieval_chunks_node_key", "retrieval_chunks", ["node_key"])

    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_knowledge_corpus_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'knowledge corpus rows are immutable'
                    USING ERRCODE = '55000';
            END;
            $$;
            """
        )
    )
    for table_name in _IMMUTABLE_TABLES:
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table_name}_prevent_mutation
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW
                EXECUTE FUNCTION prevent_knowledge_corpus_mutation();
                """
            )
        )


def downgrade() -> None:
    """移除可审计语料表和对应的不可变保护触发器。"""

    for table_name in reversed(_IMMUTABLE_TABLES):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {table_name}_prevent_mutation ON {table_name};")
        )

    op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_knowledge_corpus_mutation();"))
    op.drop_index("ix_retrieval_chunks_node_key", table_name="retrieval_chunks")
    op.drop_index("ix_retrieval_chunks_edition_key", table_name="retrieval_chunks")
    op.drop_table("retrieval_chunks")
    op.drop_index("ix_knowledge_nodes_edition_key", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
    op.drop_table("knowledge_edition_supersessions")
    op.drop_index("ix_knowledge_editions_source_key", table_name="knowledge_editions")
    op.drop_table("knowledge_editions")
    op.drop_table("knowledge_sources")
