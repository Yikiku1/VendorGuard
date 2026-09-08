"""正式语料到 KnowledgeNode 与 RetrievalChunk 解析器的单元测试.

语料是冻结的, 因此这里同时钉三类东西:
- 可回归的总量快照 (887 节点 / 274 检索块 / 6 条丢弃页脚), 语料或解析器
  任何一侧变动都会打穿这里, 强制有意识地更新而不是静默漂移;
- 12 个金标准 locator 必须唯一命中且可经检索块召回, 这是评测集与解析器
  之间的跨文件契约;
- 负向形状 (乱序条号, 未覆盖行, 三级标题, 一章两表, 分隔线缺失,
  单元格错位, 预算超限) 必须明确抛 NodeParseError, 不允许猜测兼容.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vendorguard.evidence import node_parser
from vendorguard.evidence.node_parser import (
    EditionInput,
    EditionParseResult,
    NodeParseError,
    parse_frozen_corpus,
    parse_knowledge_edition,
)
from vendorguard.evidence.schema import KnowledgeNode

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
EVAL_PATH = REPO_ROOT / "data" / "evals" / "rag_phase1.json"


@pytest.fixture(scope="module")
def frozen_results() -> dict[str, EditionParseResult]:
    """一次性解析全部冻结语料, 供本模块各用例共享."""

    return parse_frozen_corpus(KNOWLEDGE_DIR)


@pytest.fixture(scope="module")
def normalized_texts() -> dict[str, str]:
    """按版本读取归一化正文, 用于逐字区间回指核对."""

    manifest = json.loads((KNOWLEDGE_DIR / "normalized" / "manifest.json").read_text("utf-8"))
    return {
        str(entry["edition_key"]): (KNOWLEDGE_DIR / str(entry["normalized_path"])).read_text(
            "utf-8"
        )
        for entry in manifest["entries"]
    }


def _make_edition(text: str, *, edition_key: str = "probe_edition") -> EditionInput:
    """构造正文哈希自洽的最小解析输入, 供形状负向用例复用."""

    return EditionInput(
        edition_key=edition_key,
        document_title="探针语料",
        snapshot_sha256="a" * 64,
        normalized_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


def _find_node(result: EditionParseResult, locator: tuple[str, ...]) -> KnowledgeNode:
    """按定位符取唯一节点, 命中数不为 1 即失败.

    评测匹配器将来也按 node.locator == target.locator 精确相等判定, 这里
    用同一个口径把"最小路径必须唯一"提前钉进解析器契约.
    """

    matches = [node for node in result.nodes if node.locator == locator]
    assert len(matches) == 1, f"locator {locator} 应唯一命中, 实际 {len(matches)}"
    return matches[0]


def _node_by_key(result: EditionParseResult, node_key: str) -> KnowledgeNode:
    """按节点键取节点."""

    return next(node for node in result.nodes if node.node_key == node_key)


# ---------------------------------------------------------------------------
# 冻结语料总量与跨文件契约
# ---------------------------------------------------------------------------


def test_frozen_corpus_produces_pinned_totals(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """10 份语料的解析总量钉死为一次快照, 语料或解析器任何一侧变动都必须先在这里报警."""

    assert len(frozen_results) == 10
    total_nodes = sum(len(result.nodes) for result in frozen_results.values())
    total_chunks = sum(len(result.chunks) for result in frozen_results.values())
    total_dropped = sum(len(result.dropped) for result in frozen_results.values())
    assert total_nodes == 887
    assert total_chunks == 274
    assert total_dropped == 6


def test_every_gold_eval_locator_resolves_and_is_recallable(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """评测集 12 个金标准 target 均唯一命中节点, 且节点都有检索块可召回.

    这是解析器与 data/evals/rag_phase1.json 之间的跨文件契约: 条, 制度
    章节用单级定位符, 表格数据行用 (章节标题, "第N行") 两级定位符, 行号
    只数数据行. 任何一侧改名或合并都会在这里立刻暴露.
    """

    evals = json.loads(EVAL_PATH.read_text("utf-8"))
    targets = [
        (str(target["edition_key"]), tuple(target["locator"]))
        for case in evals["cases"]
        for slot in case["required_slots"]
        for target in slot["targets"]
    ]
    assert len(targets) == 12
    for edition_key, locator in targets:
        result = frozen_results[edition_key]
        node = _find_node(result, locator)
        assert any(chunk.node_key == node.node_key for chunk in result.chunks), (
            f"{edition_key}@{locator} 命中了节点但没有检索块, 无法被召回"
        )


def test_all_nodes_and_chunks_are_verbatim_and_hash_consistent(
    frozen_results: dict[str, EditionParseResult],
    normalized_texts: dict[str, str],
) -> None:
    """每个节点 body 逐字等于归一化正文切片且哈希可复算, 检索块同理.

    节点绑定的是 normalized_sha256 对应文本, 因此测试独立重读文件按字符
    区间回指, 而不是信任解析器内部状态.
    """

    for edition_key, result in frozen_results.items():
        text = normalized_texts[edition_key]
        node_by_key = {node.node_key: node for node in result.nodes}
        for node in result.nodes:
            assert text[node.char_start : node.char_end] == node.body, (
                f"{edition_key}@{node.locator} 区间回指失败"
            )
            assert hashlib.sha256(node.body.encode("utf-8")).hexdigest() == node.body_sha256, (
                f"{edition_key}@{node.locator} 哈希复算失败"
            )
        for chunk in result.chunks:
            assert text[chunk.char_start : chunk.char_end] == chunk.display_text
            assert chunk.display_text in chunk.search_text
            assert chunk.node_body_sha256 == node_by_key[chunk.node_key].body_sha256
            assert chunk.edition_key == edition_key


# ---------------------------------------------------------------------------
# 法规路径: 条, 款, 项与章, 节
# ---------------------------------------------------------------------------


def test_regulation_chunk_granularity_is_article_only(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """法规只为条生成检索块, 章, 节, 款, 项节点不生成, 防止 BM25 双计.

    款/项子节点服务精确引用定位, 检索命中一律落到条, 与金标准口径一致.
    """

    result = frozen_results["certification_and_accreditation_regulation_v2023"]
    assert len(result.chunks) == 77
    chunked_keys = {chunk.node_key for chunk in result.chunks}
    for node in result.nodes:
        if node.node_type == "article":
            assert node.node_key in chunked_keys
        else:
            assert node.node_key not in chunked_keys, (
                f"{node.node_type} 节点 {node.node_key} 不应有检索块"
            )


def test_article_locator_is_minimal_path_with_chapter_lineage(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """条文定位符不带章前缀 (与评测集逐字一致), 章只作为谱系父级存在.

    交接文档里 ("第四章", "第十九条") 式两级样例是 Schema 层的捏造数据,
    不是解析器契约; 冻结评测集要求 ("第二十七条",) 单级精确命中.
    """

    result = frozen_results["certification_and_accreditation_regulation_v2023"]
    article = _find_node(result, ("第二十七条",))
    assert article.node_type == "article"
    chapter = _find_node(result, ("第三章",))
    assert chapter.node_type == "chapter"
    assert article.parent_node_key == chapter.node_key


def test_half_width_item_chain_under_article(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """半角括号 (一) 式项生成 条-款-项 三级链, 父级键逐层回指.

    认证认可条例第十条的款1 下挂五个半角项, 是"项挂在紧邻前一款之下"
    语义的正例.
    """

    result = frozen_results["certification_and_accreditation_regulation_v2023"]
    article = _find_node(result, ("第十条",))
    paragraph = _find_node(result, ("第十条", "第一款"))
    item = _find_node(result, ("第十条", "第一款", "(一)"))
    assert article.node_type == "article"
    assert paragraph.parent_node_key == article.node_key
    assert item.parent_node_key == paragraph.node_key
    assert item.node_type == "item"
    assert "法人资格" in item.body


def test_full_width_item_label_normalized_in_locator_but_verbatim_in_body(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """全角括号式项: 定位符统一为半角括号, 节点 body 仍逐字保留全角原文.

    同一部公示条例 2024 版内半角与全角括号混用 (第六条半角, 第十六条全角),
    定位符必须跨排版稳定, 引用原文必须与冻结字节一致, 两者刻意分离.
    """

    result = frozen_results["enterprise_information_publicity_regulation_v2024"]
    item = _find_node(result, ("第十六条", "第一款", "(一)"))
    assert item.body.startswith("\uff08一\uff09")


def test_multi_paragraph_article_numbering(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """条内自然段按 第一款, 第二款 递增编号, 项不占用款号.

    公示条例 2024 第六条 = 引语段 + 五个半角项 + 收尾段: 项挂款1, 收尾段
    是款2 而不是款7, 这一形状钉住中文法规"款按自然段"的编号口径.
    """

    result = frozen_results["enterprise_information_publicity_regulation_v2024"]
    second = _find_node(result, ("第六条", "第二款"))
    assert second.body == "前款规定的企业信息应当自产生之日起20个工作日内予以公示。"
    _find_node(result, ("第六条", "第一款", "(五)"))
    with pytest.raises(AssertionError):
        _find_node(result, ("第六条", "第六款"))


def test_article_chunk_search_text_carries_title_and_chapter_context(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """法规条级检索块的 search_text 以 法规标题+章标签 为前缀, 原文随后."""

    result = frozen_results["certification_and_accreditation_regulation_v2023"]
    article = _find_node(result, ("第二十七条",))
    chunk = next(chunk for chunk in result.chunks if chunk.node_key == article.node_key)
    assert chunk.search_text.startswith("中华人民共和国认证认可条例 第三章\n" + chunk.display_text)


# ---------------------------------------------------------------------------
# 制度 Markdown 路径: 章节与表格
# ---------------------------------------------------------------------------


def test_policy_section_locators_and_verbatim_shapes(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """内部制度按 ## 标题产出单级章节定位符, body 不含块尾空白行."""

    result = frozen_results["demo_supplier_admission_policy_v2"]
    headings = [
        "适用范围",
        "正常准入条件",
        "必需材料要求",
        "资质与证书材料的有效性",
        "审批权限",
        "施行与废止",
    ]
    sections = [node for node in result.nodes if node.node_type == "section"]
    assert [node.locator for node in sections] == [(h,) for h in headings]
    last = _find_node(result, ("施行与废止",))
    assert last.body.endswith("不影响现行版本下的重新评估。")
    assert not last.body.endswith("\n")


def test_table_row_numbering_counts_data_rows_only(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """表格数据行行号从 1 起且只数数据行, 表头与分隔线不成行节点.

    金标准 (关键物料必需材料, 第2行) 必须是质量证书行, (普通工业部件
    必需材料, 第4行) 必须是质量证书非必需行, 与两道评测题语义吻合.
    """

    result = frozen_results["demo_supplier_required_documents_policy_v3"]
    row_2 = _find_node(result, ("关键物料必需材料", "第2行"))
    assert "质量证书" in row_2.body
    assert row_2.body.startswith("| critical_material")
    row_4 = _find_node(result, ("普通工业部件必需材料", "第4行"))
    assert "质量证书" in row_4.body
    assert "非必需" in row_4.body
    for node in result.nodes:
        if node.node_type == "table_row":
            assert "品类编码" not in node.body, "表头行不应成为数据行节点"


def test_table_lineage_and_span_containment(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """表格谱系为 文档-章节-表格-数据行, 行区间包含于表格区间."""

    result = frozen_results["demo_supplier_required_documents_policy_v3"]
    row = _find_node(result, ("关键物料必需材料", "第1行"))
    table = _find_node(result, ("关键物料必需材料", "表格"))
    section = _find_node(result, ("关键物料必需材料",))
    document = _node_by_key(result, section.parent_node_key or "")
    assert row.parent_node_key == table.node_key
    assert table.parent_node_key == section.node_key
    assert document.node_type == "document"
    assert "品类编码" in table.body, "表格节点正文应含表头与分隔线"
    assert table.char_start <= row.char_start and row.char_end <= table.char_end


def test_sections_with_tables_produce_no_section_level_chunk(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """含表格章节不出章节级检索块, 纯散文章节出; 行级块已携带标题与表头."""

    result = frozen_results["demo_supplier_required_documents_policy_v3"]
    chunked_keys = {chunk.node_key for chunk in result.chunks}
    table_section = _find_node(result, ("关键物料必需材料",))
    prose_section = _find_node(result, ("材料齐备性判定口径",))
    assert table_section.node_key not in chunked_keys
    assert prose_section.node_key in chunked_keys
    assert len(result.chunks) == 2 + 14  # 2 个散文节 + 14 个数据行


def test_table_row_chunk_search_text_flattens_header_and_cells(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """行级检索块的 search_text 同时含原始行, 章节标题与 表头:单元格 展开."""

    result = frozen_results["demo_supplier_required_documents_policy_v3"]
    row = _find_node(result, ("关键物料必需材料", "第2行"))
    chunk = next(chunk for chunk in result.chunks if chunk.node_key == row.node_key)
    assert chunk.display_text in chunk.search_text
    assert "关键物料必需材料" in chunk.search_text
    assert "材料名称:质量证书" in chunk.search_text


# ---------------------------------------------------------------------------
# 噪声处理与守恒不变量
# ---------------------------------------------------------------------------


def test_site_footer_noise_dropped_without_leaking_into_bodies(
    frozen_results: dict[str, EditionParseResult],
) -> None:
    """页脚噪声恰为 6 条 (3 份法规各 2 条), 且绝不混入任何节点或展示原文.

    若解析器把版权行并进施行条款的区间, body 会直接污染可引用原文;
    这里按子串逐条排查所有 body 与 display_text.
    """

    per_edition = {key: len(result.dropped) for key, result in frozen_results.items()}
    assert sum(per_edition.values()) == 6
    for edition_key in (
        "certification_and_accreditation_regulation_v2023",
        "enterprise_information_publicity_regulation_v2014",
        "enterprise_information_publicity_regulation_v2024",
    ):
        assert per_edition[edition_key] == 2
    for result in frozen_results.values():
        for dropped in result.dropped:
            assert dropped.reason == "site_footer"
            assert "版权所有" in dropped.text or "Copyright" in dropped.text
        for node in result.nodes:
            assert "版权所有" not in node.body
            assert "ICP备" not in node.body
        for chunk in result.chunks:
            assert "版权所有" not in chunk.display_text


# ---------------------------------------------------------------------------
# 负向形状: 明确失败, 不猜测
# ---------------------------------------------------------------------------


def test_edition_input_rejects_drifted_text_hash() -> None:
    """正文与声明的 normalized_sha256 不一致时拒绝构造输入."""

    with pytest.raises(ValidationError):
        EditionInput(
            edition_key="probe_edition",
            document_title="探针语料",
            snapshot_sha256="a" * 64,
            normalized_sha256="b" * 64,
            text="第一条 正文。",
        )


def test_article_number_must_increase_strictly() -> None:
    """条号乱序出现即报错, 提示上游清洗或语料被改动."""

    edition = _make_edition("第一条 正文。\n\n第三条 更多。\n\n第二条 乱序。\n")
    with pytest.raises(NodeParseError, match="递增"):
        parse_knowledge_edition(edition)


def test_unknown_shape_is_rejected() -> None:
    """既不以一级标题也不以 第一条 开头的正文形状直接拒绝, 不做启发式."""

    edition = _make_edition("本条例说明性前言。\n\n第一条 正文。\n")
    with pytest.raises(NodeParseError, match="形状"):
        parse_knowledge_edition(edition)


def test_line_between_title_and_first_section_is_uncovered_and_rejected() -> None:
    """文档标题与首个 ## 章节之间的有内容行没有归属, 必须打穿守恒不变量."""

    edition = _make_edition("# 标题\n\n游离说明行。\n\n## 甲\n\n内容。\n")
    with pytest.raises(NodeParseError, match="覆盖"):
        parse_knowledge_edition(edition)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# 标题\n\n## 甲\n\n内容。\n\n### 乙\n\n更细。\n", "只允许一级与二级标题"),
        ("# 标题\n\n## 甲\n\n内容。\n\n# 又一个标题\n\n杂文。\n", "只允许一个一级标题"),
    ],
    ids=["h3", "second-h1"],
)
def test_unexpected_heading_depth_is_rejected(text: str, message: str) -> None:
    """三级标题与第二个一级标题超出冻结形状, 明确报错."""

    with pytest.raises(NodeParseError, match=message):
        parse_knowledge_edition(_make_edition(text))


def test_two_tables_in_one_section_are_rejected() -> None:
    """一节两表破坏 (标题, 表格) 与 (标题, 第N行) 的定位唯一性."""

    text = (
        "# 标题\n\n## 甲\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "过渡散文。\n\n| c | d |\n| --- | --- |\n| 3 | 4 |\n"
    )
    with pytest.raises(NodeParseError, match="多张表格"):
        parse_knowledge_edition(_make_edition(text))


def test_table_without_separator_row_is_rejected() -> None:
    """Markdown 表格缺分隔线说明清洗退化, 按行切分会静默错位."""

    text = "# 标题\n\n## 甲\n\n| a | b |\n| 1 | 2 |\n| 3 | 4 |\n"
    with pytest.raises(NodeParseError, match="分隔线"):
        parse_knowledge_edition(_make_edition(text))


def test_table_row_with_inconsistent_cell_count_is_rejected() -> None:
    """数据行单元格数与表头不一致时拒绝, 防止列错位进入引用."""

    text = "# 标题\n\n## 甲\n\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    with pytest.raises(NodeParseError, match="单元格数"):
        parse_knowledge_edition(_make_edition(text))


def test_chunk_budget_guard_rejects_oversized_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过 rerank 字符预算的叶级块必须显式失败, 而不是静默截断."""

    monkeypatch.setattr(node_parser, "MAX_CHUNK_CHARS", 5)
    edition = _make_edition("第一条 这段正文故意超出被压缩后的字符预算。\n")
    with pytest.raises(NodeParseError, match="预算"):
        parse_knowledge_edition(edition)


def test_stray_paragraph_inside_article_becomes_next_kuan() -> None:
    """条内后续自然段是"款"而不是噪声: 孤行并入当前条并递增款号.

    与 test_line_between_title_and_first_section 的差别在于这里段落落在
    条文区间内, 属于合法结构; 区间外才触发守恒报错.
    """

    edition = _make_edition("第一条 首段。\n\n次段。\n")
    result = parse_knowledge_edition(edition)
    article = _find_node(result, ("第一条",))
    second = _find_node(result, ("第一条", "第二款"))
    assert second.parent_node_key == article.node_key
    assert "次段。" in article.body
