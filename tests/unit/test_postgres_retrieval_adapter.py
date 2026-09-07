"""PostgreSQL 检索适配器的 SQL 资格过滤与策略边界测试。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.dialects import postgresql

from vendorguard.evidence.retrieval import PostgresRetrievalAdapter, RetrievalQuery


def _query(**overrides: object) -> RetrievalQuery:
    """构造默认的直接制度检索请求。"""

    data: dict[str, object] = {
        "text": "关键物料质量证书",
        "as_of": date(2026, 9, 1),
        "intent": "direct_policy",
    }
    data.update(overrides)
    return RetrievalQuery.model_validate(data)


@pytest.mark.parametrize("strategy", ["structured", "bm25", "vector"])
def test_all_strategies_compile_the_same_hard_eligibility_filters(strategy: str) -> None:
    """精确匹配、BM25 与余弦排序共享时间、角色和条件过滤谓词。"""

    adapter = PostgresRetrievalAdapter()
    embedding = (0.0,) * 1024 if strategy == "vector" else None
    statement = adapter._statement(
        query=_query(
            intent="conditional_reference",
            established_conditions=frozenset({"product_in_ccc_directory"}),
        ),
        strategy=strategy,  # type: ignore[arg-type]
        limit=20,
        query_embedding=embedding,
    )
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()

    assert "knowledge_editions.role in" in sql
    assert "knowledge_editions.effective_from <=" in sql
    assert (
        "knowledge_editions.effective_until is null or knowledge_editions.effective_until >=" in sql
    )
    assert "knowledge_editions.required_conditions <@" in sql
    if strategy == "structured":
        assert "strpos" in sql
    elif strategy == "bm25":
        assert "paradedb.score" in sql
        assert "@@@" in sql
    else:
        assert "<=>" in sql


def test_vector_strategy_rejects_missing_or_wrong_dimension_embedding() -> None:
    """向量召回在发 SQL 前拒绝缺失或维度错误的查询向量。"""

    adapter = PostgresRetrievalAdapter()

    with pytest.raises(ValueError, match="必须提供查询向量"):
        adapter._statement(
            query=_query(),
            strategy="vector",
            limit=20,
            query_embedding=None,
        )

    with pytest.raises(ValueError, match="1024"):
        adapter._statement(
            query=_query(),
            strategy="vector",
            limit=20,
            query_embedding=(0.0,) * 512,
        )
