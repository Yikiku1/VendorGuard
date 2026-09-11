"""本地检索与版本过滤的单元测试."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vendorguard.retrieval import (
    KnowledgeIndex,
    RetrievalError,
    load_knowledge_index,
)

KNOWLEDGE_DIR = Path("data/knowledge")


@pytest.fixture(scope="module")
def index() -> KnowledgeIndex:
    """从冻结语料构建一次索引, 模块内复用."""

    return load_knowledge_index(KNOWLEDGE_DIR)


def test_index_covers_frozen_corpus(index: KnowledgeIndex) -> None:
    """索引块来自全部冻结版本, 数量与解析结果一致."""

    # 一期语料 10 个版本共 274 个检索块, 这里只断言非空与规模下界
    assert len(index) >= 200


def test_search_returns_citations_with_locators(index: KnowledgeIndex) -> None:
    """检索返回可引用的版本, 定位符与原文片段."""

    hit = index.search(
        "供应商准入时营业执照需要与申请表核对哪些字段?",
        as_of=date(2026, 9, 1),
    )

    assert hit.citations, "现行制度问题应当命中片段"
    top = hit.citations[0]
    assert top.edition_key == "demo_supplier_admission_policy_v2"
    assert top.locator == ("正常准入条件",)
    assert top.as_label() == "demo_supplier_admission_policy_v2@正常准入条件"
    assert "正常准入条件" in top.body


def test_search_filters_retired_edition(index: KnowledgeIndex) -> None:
    """已废止版本在参考日期之后不进入候选集, 结构性挡住旧版误用."""

    hit = index.search(
        "旧版供应商准入管理办法要求提交哪些正常准入材料?",
        as_of=date(2026, 9, 1),
    )

    returned = {citation.edition_key for citation in hit.citations}
    assert "demo_supplier_admission_policy_v1" not in returned
    assert "demo_supplier_admission_policy_v1" in hit.filtered_edition_keys


def test_search_returns_historical_edition_for_earlier_date(index: KnowledgeIndex) -> None:
    """参考日期落在旧版生效期内时, 旧版才是正确依据."""

    hit = index.search(
        "旧版供应商准入管理办法要求提交哪些正常准入材料?",
        as_of=date(2025, 12, 31),
    )

    returned = {citation.edition_key for citation in hit.citations}
    assert "demo_supplier_admission_policy_v1" in returned
    assert "demo_supplier_admission_policy_v2" in hit.filtered_edition_keys


def test_search_respects_limit(index: KnowledgeIndex) -> None:
    """返回条数受 limit 约束."""

    hit = index.search("供应商准入材料", as_of=date(2026, 9, 1), limit=2)

    assert len(hit.citations) <= 2


def test_search_rejects_blank_query(index: KnowledgeIndex) -> None:
    """空查询是调用错误, 明确抛出而不是返回任意结果."""

    with pytest.raises(RetrievalError):
        index.search("   ", as_of=date(2026, 9, 1))


def test_search_rejects_non_positive_limit(index: KnowledgeIndex) -> None:
    """非正数上限是调用错误."""

    with pytest.raises(RetrievalError):
        index.search("供应商", as_of=date(2026, 9, 1), limit=0)


def test_citation_body_is_verbatim_corpus_text(index: KnowledgeIndex) -> None:
    """引用片段必须能在冻结正文里原样找到, 保证出处可回放."""

    hit = index.search("关键物料 质量证书", as_of=date(2026, 9, 1))
    assert hit.citations

    normalized = (
        KNOWLEDGE_DIR / "normalized" / "demo_supplier_required_documents_policy_v3.txt"
    ).read_text(encoding="utf-8")

    for citation in hit.citations:
        if citation.edition_key != "demo_supplier_required_documents_policy_v3":
            continue
        # 行首标记与空白差异不参与比对, 只要求正文可定位
        probe = citation.body.strip().splitlines()[-1].strip()
        assert probe in normalized


def test_is_effective_uses_closed_interval(index: KnowledgeIndex) -> None:
    """生效期是闭区间: 起止当天都算生效, 次日才算失效."""

    # 2024 版公示条例自 2024-05-01 起生效
    assert index.is_effective("enterprise_information_publicity_regulation_v2024", date(2024, 5, 1))
    assert not index.is_effective(
        "enterprise_information_publicity_regulation_v2024", date(2024, 4, 30)
    )
    # 旧版制度到 2025-12-31 为止
    assert index.is_effective("demo_supplier_admission_policy_v1", date(2025, 12, 31))
    assert not index.is_effective("demo_supplier_admission_policy_v1", date(2026, 1, 1))


def test_is_effective_is_false_for_unknown_edition(index: KnowledgeIndex) -> None:
    """未知版本不生效, 不会被误当成长期有效."""

    assert not index.is_effective("no_such_edition", date(2026, 9, 1))


def test_empty_index_search_fails(tmp_path: Path) -> None:
    """空索引检索时报错, 而不是静默返回空结果."""

    empty = KnowledgeIndex()
    with pytest.raises(RetrievalError):
        empty.search("任意问题", as_of=date(2026, 9, 1))
