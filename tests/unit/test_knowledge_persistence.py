"""可审计 RAG 持久化模型的结构契约。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.schema import CreateTable

from vendorguard.database import Base
from vendorguard.evidence.persistence import (
    KnowledgeEditionRecord,
    KnowledgeEditionSupersession,
    KnowledgeNodeRecord,
    KnowledgeSourceRecord,
    RetrievalChunkRecord,
)


def _postgresql_ddl(table_name: str) -> str:
    """编译模型 DDL, 不建立数据库连接。"""

    engine = create_engine("postgresql+asyncpg://")
    try:
        return str(
            CreateTable(Base.metadata.tables[table_name]).compile(dialect=engine.dialect)
        ).lower()
    finally:
        engine.dispose()


def test_auditable_retrieval_tables_are_registered_in_shared_metadata() -> None:
    """迁移自动比对必须能发现全部来源, 版本, 节点和检索投影表。"""

    assert set(
        {
            KnowledgeSourceRecord.__tablename__,
            KnowledgeEditionRecord.__tablename__,
            KnowledgeEditionSupersession.__tablename__,
            KnowledgeNodeRecord.__tablename__,
            RetrievalChunkRecord.__tablename__,
        }
    ) <= set(Base.metadata.tables)


def test_edition_and_node_tables_keep_replay_fields_and_foreign_keys() -> None:
    """版本、节点和检索投影必须保留回放所需哈希, 区间与引用关系。"""

    edition_columns = KnowledgeEditionRecord.__table__.c
    node_columns = KnowledgeNodeRecord.__table__.c
    chunk_columns = RetrievalChunkRecord.__table__.c

    assert {
        "snapshot_sha256",
        "normalized_sha256",
        "effective_from",
        "role",
        "required_conditions",
        "canonical_url",
        "retrieved_at",
    } <= set(edition_columns.keys())
    assert {"locator", "char_start", "char_end", "body", "body_sha256"} <= set(node_columns.keys())
    assert {"node_body_sha256", "display_text", "search_text", "embedding"} <= set(
        chunk_columns.keys()
    )
    assert {
        foreign_key.target_fullname for foreign_key in node_columns["edition_key"].foreign_keys
    } == {"knowledge_editions.edition_key"}
    assert {
        foreign_key.target_fullname for foreign_key in chunk_columns["node_key"].foreign_keys
    } == {"knowledge_nodes.node_key"}


def test_chunk_embedding_and_replay_text_constraints_compile_for_postgresql() -> None:
    """向量维度和可回放展示文本必须由数据库结构表达。"""

    ddl = _postgresql_ddl(RetrievalChunkRecord.__tablename__)

    assert "vector(1024)" in ddl
    assert "retrieval_chunk_display_text_length" in ddl
    assert "retrieval_chunk_search_includes_display" in ddl


def test_edition_supersession_uses_an_immutable_composite_identity() -> None:
    """一个新版可替代多个前身, 同一替代关系不得重复。"""

    primary_key_columns = [
        column.name for column in KnowledgeEditionSupersession.__table__.primary_key
    ]

    assert primary_key_columns == ["successor_edition_key", "predecessor_edition_key"]
