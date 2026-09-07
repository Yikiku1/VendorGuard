"""从规范正文自动生成可回放 KnowledgeNode 的契约测试。"""

from __future__ import annotations

import json
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

from vendorguard.evidence.node_parser import parse_knowledge_nodes


def _parse(text: str):
    """使用固定版本元数据解析简化的法规或制度正文。"""

    return parse_knowledge_nodes(
        edition_key="demo_edition_v1",
        snapshot_sha256="a" * 64,
        normalized_sha256=sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


def _assert_verbatim_non_overlapping(nodes, text: str) -> None:
    """每个节点必须回指精确正文区间, 且结构节点之间不得重叠。"""

    spans = sorted((node.char_start, node.char_end) for node in nodes)
    assert len(spans) == len(set(spans))

    for node in nodes:
        assert text[node.char_start : node.char_end] == node.body
        assert node.body_sha256 == sha256(node.body.encode("utf-8")).hexdigest()
        assert node.normalized_sha256 == sha256(text.encode("utf-8")).hexdigest()

    assert all(left[1] <= right[0] for left, right in pairwise(spans))


def test_parser_builds_non_overlapping_nodes_for_regulation_hierarchy() -> None:
    """法规按章, 条, 正文和项拆分, 叶节点带完整的结构定位符。"""

    text = (
        "第一章 总则\n\n"
        "第一条 为了规范供应商准入活动, 制定本办法.\n\n"
        "第二条 供应商应当提交下列材料:\n\n"
        "(一) 营业执照.\n\n"
        "(二) 报价单.\n"
    )

    nodes = _parse(text)
    node_by_locator = {node.locator: node for node in nodes}

    assert {node.node_type for node in nodes} == {"chapter", "article", "paragraph", "item"}
    assert node_by_locator[("第一章 总则",)].node_type == "chapter"
    assert node_by_locator[("第一章 总则", "第一条")].node_type == "article"
    assert (
        node_by_locator[("第一章 总则", "第一条", "正文")].body
        == "为了规范供应商准入活动, 制定本办法."
    )
    assert node_by_locator[("第一章 总则", "第二条", "(一)")].body == "(一) 营业执照."
    assert node_by_locator[("第一章 总则", "第二条", "(二)")].body == "(二) 报价单."
    _assert_verbatim_non_overlapping(nodes, text)


def test_parser_builds_table_row_nodes_from_internal_policy_markdown() -> None:
    """内部制度表格保留标题路径, 表头与每行材料要求分别形成节点。"""

    text = (
        "# 分品类必需准入材料清单\n\n"
        "## 普通工业部件必需材料\n\n"
        "| 品类编码 | 材料名称 | 是否必需 |\n"
        "| --- | --- | --- |\n"
        "| standard_components | 营业执照 | 必需 |\n"
        "| standard_components | 质量证书 | 非必需 |\n"
    )

    nodes = _parse(text)
    table_nodes = [node for node in nodes if node.node_type == "table"]
    row_nodes = [node for node in nodes if node.node_type == "table_row"]

    assert len(table_nodes) == 1
    assert table_nodes[0].locator == ("分品类必需准入材料清单", "普通工业部件必需材料", "表格")
    assert [node.body for node in row_nodes] == [
        "| standard_components | 营业执照 | 必需 |",
        "| standard_components | 质量证书 | 非必需 |",
    ]
    assert all(node.parent_node_key == table_nodes[0].node_key for node in row_nodes)
    _assert_verbatim_non_overlapping(nodes, text)


def test_parser_uses_stable_keys_for_identical_input() -> None:
    """同一版本正文重复解析必须得到相同节点键与定位顺序。"""

    text = "第一条 供应商应当提交营业执照.\n"

    first_nodes = _parse(text)
    second_nodes = _parse(text)

    assert [(node.node_key, node.locator) for node in first_nodes] == [
        (node.node_key, node.locator) for node in second_nodes
    ]


def test_parser_replays_all_frozen_normalized_corpus_nodes() -> None:
    """一期全部正式正文均可解析, 材料清单中的数据行不会在解析时丢失。"""

    repository_root = Path(__file__).resolve().parents[2]
    knowledge_root = repository_root / "data" / "knowledge"
    manifest = json.loads(
        (knowledge_root / "normalized" / "manifest.json").read_text(encoding="utf-8")
    )
    all_nodes = {}

    for entry in manifest["entries"]:
        text = (knowledge_root / entry["normalized_path"]).read_text(encoding="utf-8")
        assert sha256(text.encode("utf-8")).hexdigest() == entry["normalized_sha256"]

        nodes = parse_knowledge_nodes(
            edition_key=entry["edition_key"],
            snapshot_sha256=entry["snapshot_sha256"],
            normalized_sha256=entry["normalized_sha256"],
            text=text,
        )
        assert nodes
        _assert_verbatim_non_overlapping(nodes, text)
        all_nodes[entry["edition_key"]] = nodes

    material_rows = [
        node.body
        for node in all_nodes["demo_supplier_required_documents_policy_v3"]
        if node.node_type == "table_row"
    ]
    assert "| standard_components | 营业执照 | 必需 | 截止日当天仍视为有效 |" in material_rows
    assert "| critical_material | 质量证书 | 必需 | 剩余有效期不少于 90 天 |" in material_rows
