"""知识库语料与既有契约之间的一致性回归测试.

这一组不重复验证 Schema 字段本身(那是 test_knowledge_schema.py 的职责), 只钉跨文件事实:
案例声明的引用目标是否存在、版本血缘是否闭合、演示基准日的生效判定是否与案例期望一致、
品类必需材料集合是否仍让案例期望成立、文件名是否等于 document_key、章节是否都有定位锚点.

之所以值得单独测: 语料是纯数据, 改一行不会触发任何编译或类型错误, 一旦与案例期望或规则配置
脱节, 只会在很后面的评测里以"检索结果看起来没问题"的形式暴露, 那时已经分不清是语料错还是检索错.
"""

from datetime import date
from pathlib import Path

from vendorguard.evidence.schema import (
    KnowledgeDocument,
    KnowledgeSection,
    load_knowledge_catalog,
    load_knowledge_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"

# 与 data/demo/cases/*.yaml 的 reference_date 保持一致, 评测的 as_of 也以它为默认基准.
DEMO_REFERENCE_DATE = date(2026, 9, 1)


def _catalog() -> dict[str, KnowledgeDocument]:
    """加载真实语料目录, 返回 document_key 到文档模型的映射."""

    return load_knowledge_catalog(KNOWLEDGE_DIR)


def _is_effective(document: KnowledgeDocument) -> bool:
    """判断文档在演示基准日是否生效, 截止日当天仍视为有效."""

    if document.effective_from > DEMO_REFERENCE_DATE:
        return False

    return document.effective_until is None or document.effective_until >= DEMO_REFERENCE_DATE


def _required_materials(table: KnowledgeSection) -> set[str]:
    """从表格章节里取出标注为"必需"的材料名称.

    这里刻意不复用第 3 步才要写的切块函数: 测试解析保持最朴素的竖线分割, 这样当切块实现
    出错时, 本测试仍能独立判断语料本身的期望值。列顺序为: 品类 | 材料 | 是否必需 | 有效期。
    """

    required: set[str] = set()
    for line in table.body.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[2] == "必需":
            required.add(cells[1])

    return required


def test_case_contract_reference_targets_exist() -> None:
    """data/demo/cases 声明的 expected_reference 必须能在语料里定位到同名文档与章节.

    normal_admission.yaml 指向 demo_supplier_admission_policy_v2 的 normal_admission 章节,
    supplement_review.yaml 指向 demo_supplier_quote_policy_v1 的 quote_deviation_review 章节。
    引用必须落在现行版本上: 检索按案例 reference_date 做生效期硬过滤, 落在已废止版本上的
    期望在数学上永远召不回。少任何一个目标, Day 7 兑现 evidence_reviewing 时就无据可引。
    """

    catalog = _catalog()

    admission = catalog["demo_supplier_admission_policy_v2"]
    assert "normal_admission" in {section.key for section in admission.sections}

    quote = catalog["demo_supplier_quote_policy_v1"]
    assert "quote_deviation_review" in {section.key for section in quote.sections}

    # 废止版本仍保留同名章节: 它是"旧口径不该被召回"这类题目的负样本, 不是冗余。
    retired = catalog["demo_supplier_admission_policy_v1"]
    assert "normal_admission" in {section.key for section in retired.sections}


def test_version_lineage_is_closed() -> None:
    """版本血缘必须是列表, 且指向目录里真实存在的前身.

    自制侧保留一组 1:1 版本对; 1:N 的真实废止链由公开公文语料承担, 其列表形状已在 Schema 层
    由 test_catalog_accepts_multiple_predecessors 守住, 这里只验本目录自身的血缘闭合。
    """

    catalog = _catalog()

    assert catalog["demo_supplier_admission_policy_v2"].supersedes == [
        "demo_supplier_admission_policy_v1"
    ]
    assert catalog["demo_supplier_required_documents_policy_v3"].supersedes == []


def test_effective_state_matches_demo_reference_date() -> None:
    """基准日上现行文档生效、已废止文档不生效, 这是生效期过滤评测的前提."""

    catalog = _catalog()

    assert _is_effective(catalog["demo_supplier_admission_policy_v2"])
    assert _is_effective(catalog["demo_supplier_required_documents_policy_v3"])
    assert _is_effective(catalog["demo_supplier_quote_policy_v1"])
    assert not _is_effective(catalog["demo_supplier_admission_policy_v1"])


def test_standard_components_required_set_keeps_case_expected_true() -> None:
    """普通工业部件的必需材料必须仍是演示案例已提供材料的子集.

    normal_admission.yaml 期望 category_required_documents_complete 为 true, 而它只登记了
    营业执照、报价单、交付记录与质量证书四类材料。若语料把必需项改成案例没有的材料类型
    (例如把质量证书改成必需), 该期望就变成假值, 而这条改动不会让任何 Python 代码报错。
    """

    document = _catalog()["demo_supplier_required_documents_policy_v3"]
    table = next(
        section
        for section in document.sections
        if section.key == "standard_components_required_documents"
    )

    assert _required_materials(table) == {"营业执照", "报价单", "交付记录"}


def test_document_key_matches_file_name() -> None:
    """文件名必须等于 document_key: 复制旧文件改内容却忘改键, 会让血缘指向错误的文档."""

    for path in sorted(KNOWLEDGE_DIR.rglob("*.y*ml")):
        document = load_knowledge_document(path)

        assert document.document_key == path.stem, f"{path.name} 的键与文件名不一致"


def test_every_corpus_section_has_locator_anchor() -> None:
    """每个章节都要有定位锚点: 章节键必备, 页码有则必须合法.

    原来这条断言"必须有页码", 那是把 PRD 的"章节或页码"误读成"章节与页码": 法规条文和网页
    根本没有页码, 硬要页码等于用测试挡死全部真实公开语料。页码保留为可选, 但一旦填写必须 >= 1。
    """

    for document in _catalog().values():
        for section in document.sections:
            assert section.key, f"{document.document_key} 存在空章节键"
            if section.page is not None:
                assert section.page >= 1, f"{document.document_key}#{section.key} 页码非法"


def test_every_corpus_document_declares_scope_notes() -> None:
    """每篇语料都要带边界声明.

    真实公开文书最容易出的事故不是抓错字, 而是把不适用于本主体的条款当成公司准入依据:
    例如《企业信息公示暂行条例》第十九条约束的是政府部门在政府采购与工程招投标中的行为,
    不是民营采购方的义务。honest_scope_notes 非空是这类误用的第一道强制自述。
    """

    for document in _catalog().values():
        assert document.honest_scope_notes, f"{document.document_key} 缺少边界声明"


def test_external_documents_are_not_marked_synthetic() -> None:
    """公开公文类语料必须声明非自制来源, 防止把真实法规伪装成自制内容绕过出处校验."""

    for document in _catalog().values():
        if document.authority == "internal":
            assert document.source_origin == "synthetic"
            assert document.synthetic_data is True
        else:
            assert document.source_origin != "synthetic"
            assert document.source_url
            assert document.retrieved_at


def test_external_sections_are_verbatim_from_raw_snapshot() -> None:
    """公开来源语料的每个章节正文, 必须能在同名原文快照里逐字找到.

    这是把"不许编造法条"变成机器判定: source_url 只是自我声明, 快照才是可核对的证据, 而语料
    正文与快照逐字一致才能保证 YAML 里没有掺进凭记忆补写的内容。一旦某条断言失败, 说明要么
    转抄时改了字, 要么该章节根本不是从该来源来的——两种情况都必须改语料而不是改测试。
    """

    raw_dir = KNOWLEDGE_DIR / "raw"

    for document in _catalog().values():
        if document.source_origin == "synthetic":
            continue

        snapshot = raw_dir / f"{document.document_key}.md"
        assert snapshot.exists(), f"{document.document_key} 缺少原文快照 {document.document_key}.md"

        snapshot_text = snapshot.read_text(encoding="utf-8")
        assert document.source_url is not None
        assert document.retrieved_at is not None
        assert document.source_url in snapshot_text, (
            f"{document.document_key} 的快照未记录 YAML 声明的 source_url"
        )
        assert document.retrieved_at.isoformat() in snapshot_text, (
            f"{document.document_key} 的快照未记录抓取日期 {document.retrieved_at}"
        )

        for section in document.sections:
            assert section.body.strip() in snapshot_text, (
                f"{document.document_key}#{section.key} 正文在原文快照中找不到逐字对应"
            )
