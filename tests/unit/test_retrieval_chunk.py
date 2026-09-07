"""RetrievalChunk 的检索投影与引用边界测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vendorguard.evidence.schema import RetrievalChunk


def _chunk_data(**overrides: object) -> dict[str, object]:
    """返回一个绑定法规条文节点的检索投影。"""

    display_text = "第十九条 政府部门应当建立信用约束机制."
    data: dict[str, object] = {
        "chunk_key": "article_19_main",
        "edition_key": "enterprise_information_publicity_regulation_v2024",
        "node_key": "article_19",
        "snapshot_sha256": "a" * 64,
        "normalized_sha256": "b" * 64,
        "node_body_sha256": "c" * 64,
        "char_start": 1280,
        "char_end": 1280 + len(display_text),
        "display_text": display_text,
        "search_text": f"第四章 第十九条 {display_text} 企业信息公示",
    }
    data.update(overrides)
    return data


def test_chunk_binds_a_single_node_but_keeps_search_projection_separate() -> None:
    """检索块绑定节点, 但搜索文本可以补充结构上下文和检索词。"""

    chunk = RetrievalChunk.model_validate(_chunk_data())

    assert chunk.edition_key == "enterprise_information_publicity_regulation_v2024"
    assert chunk.node_key == "article_19"
    assert chunk.char_end == chunk.char_start + len(chunk.display_text)
    assert chunk.display_text in chunk.search_text
    assert chunk.search_text != chunk.display_text


def test_chunk_is_immutable_after_generation() -> None:
    """重新切块必须产生新记录, 不得原地改变历史检索投影。"""

    chunk = RetrievalChunk.model_validate(_chunk_data())

    with pytest.raises(ValidationError):
        chunk.search_text = "替换后的检索文本"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"char_end": 1280}, "字符区间"),
        ({"char_end": 1280 + len("错误长度")}, "字符区间"),
        ({"display_text": "   "}, "文本不能为空"),
        ({"search_text": "第四章 第十九条"}, "展示原文"),
        ({"snapshot_sha256": "not-a-sha256"}, "String should match pattern"),
        ({"chunk_key": "Article-19"}, "String should match pattern"),
    ],
)
def test_chunk_rejects_incoherent_projection_metadata(
    overrides: dict[str, object], message: str
) -> None:
    """字符区间, 哈希和检索投影必须保持可回放的一致关系。"""

    with pytest.raises(ValidationError, match=message):
        RetrievalChunk.model_validate(_chunk_data(**overrides))


def test_chunk_model_does_not_expose_citation_as_a_retrieval_concern() -> None:
    """最终引用属于 KnowledgeNode, 检索块不能携带独立引用正文。"""

    with pytest.raises(ValidationError):
        RetrievalChunk.model_validate(_chunk_data(citation_text="错误字段"))
