"""add retrieval bm25 index

Revision ID: d4f8e1a6c590
Revises: c4e7a4d2b809
Create Date: 2026-09-07 14:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f8e1a6c590"
down_revision: str | Sequence[str] | None = "c4e7a4d2b809"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为检索投影建立 jieba 分词的 ParadeDB BM25 索引。"""

    op.execute(
        """
        CREATE INDEX ix_retrieval_chunks_search_bm25
        ON retrieval_chunks
        USING bm25 (chunk_key, search_text)
        WITH (
            key_field = 'chunk_key',
            text_fields = '{"search_text":{"tokenizer":{"type":"jieba"}}}'
        )
        """
    )


def downgrade() -> None:
    """移除 BM25 索引, 不影响不可变语料行。"""

    op.drop_index("ix_retrieval_chunks_search_bm25", table_name="retrieval_chunks")
