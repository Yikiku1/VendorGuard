"""知识库语料 Schema 的单元测试.

语料是后续切块、入库、检索与评测的唯一事实来源, 因此这里不测检索效果, 只测"什么样的语料
文件允许进库". 每条拒绝规则都对应一个要到后面几步才爆开的坑: 越早拒绝, 排查成本越低.

约定沿用 vendorguard.policy.schema: 未声明字段一律禁止, 跨字段规则用 model_validator 表达,
读取与校验失败统一包装成 KnowledgeLoadError.
"""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from vendorguard.evidence.schema import (
    KnowledgeLoadError,
    load_knowledge_catalog,
    load_knowledge_document,
)


def _base_document(**overrides: Any) -> dict[str, Any]:
    """构造一份合法语料字典, 只覆盖被测字段.

    刻意保留全部必填字段: 若某条测试只想验证 modality 白名单, 却因缺字段先报错, 那条测试
    就成了恒真断言, 抓不住它本来要抓的缺陷.
    """

    data: dict[str, Any] = {
        "schema_version": "1.0",
        "document_key": "demo_supplier_admission_policy_v1",
        "title": "供应商准入管理办法(演示)",
        "doc_version": "1.0",
        "supersedes": None,
        "document_type": "policy",
        "category": "supplier_admission",
        "audience": "procurement",
        "effective_from": date(2023, 1, 1),
        "effective_until": date(2025, 12, 31),
        "source_origin": "synthetic",
        "synthetic_data": True,
        "language": "zh-CN",
        "honest_scope_notes": ["内容为自制模拟制度, 不代表任何真实企业。"],
        "sections": [
            {
                "key": "normal_admission",
                "title": "正常准入条件",
                "page": 12,
                "modality": "text",
                "body": "供应商申请常规准入时, 必须提交有效的营业执照与品类要求的必需材料。",
            }
        ],
    }
    data.update(overrides)

    return data


def _section(**overrides: Any) -> dict[str, Any]:
    """构造一个合法章节, 用于只改一个字段的负向用例."""

    section: dict[str, Any] = {
        "key": "normal_admission",
        "title": "正常准入条件",
        "page": 12,
        "modality": "text",
        "body": "供应商申请常规准入时, 必须提交有效的营业执照与品类要求的必需材料。",
    }
    section.update(overrides)

    return section


def _write(path: Path, data: dict[str, Any]) -> Path:
    """把字典写成 UTF-8 YAML 文件并返回路径."""

    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    return path


def _catalog_dir(tmp_path: Path, *documents: dict[str, Any]) -> Path:
    """按序号文件名写入一组语料, 返回语料目录."""

    for index, document in enumerate(documents):
        _write(tmp_path / f"doc{index}.yaml", document)

    return tmp_path


def test_loads_valid_knowledge_document(tmp_path: Path) -> None:
    """最小合法语料应完整解析, 元数据与章节都要按声明落地."""

    document = load_knowledge_document(_write(tmp_path / "a.yaml", _base_document()))

    assert document.document_key == "demo_supplier_admission_policy_v1"
    assert document.effective_until == date(2025, 12, 31)
    assert document.sections[0].key == "normal_admission"
    assert document.sections[0].page == 12


def test_allows_open_ended_effective_period(tmp_path: Path) -> None:
    """effective_until 为 null 表示长期有效, 沿用既有 PolicyDocument 的空值做法."""

    document = load_knowledge_document(
        _write(tmp_path / "a.yaml", _base_document(effective_until=None))
    )

    assert document.effective_until is None


def test_rejects_unknown_field(tmp_path: Path) -> None:
    """未声明字段必须失败: 拼错的权限字段若被静默忽略, 语料就写了一套没人执行的规则."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(_write(tmp_path / "a.yaml", _base_document(permnission="all")))


def test_rejects_inverted_effective_period(tmp_path: Path) -> None:
    """生效上限早于下限的文档永远检索不到, 属于会静默消失的语料."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(
            _write(tmp_path / "a.yaml", _base_document(effective_until=date(2020, 1, 1)))
        )


def test_rejects_duplicate_section_key(tmp_path: Path) -> None:
    """章节键是定位符主体, 同文档内重复会让引用无法回指到唯一段落."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(
            _write(tmp_path / "a.yaml", _base_document(sections=[_section(), _section()]))
        )


def test_rejects_locator_unsafe_section_key(tmp_path: Path) -> None:
    """章节键要拼进 document_key@section:key, 中文或空格会让定位符无法解析."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(
            _write(tmp_path / "a.yaml", _base_document(sections=[_section(key="正常 准入")]))
        )


def test_rejects_unknown_modality(tmp_path: Path) -> None:
    """模态决定切块策略, 白名单之外的值不允许靠默认分支蒙混过关."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(
            _write(tmp_path / "a.yaml", _base_document(sections=[_section(modality="sheet")]))
        )


def test_rejects_table_section_without_markdown_table(tmp_path: Path) -> None:
    """标成 table 却没有表格行, 按行切块会静默退化成整段切块且没人报错."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(
            _write(
                tmp_path / "a.yaml",
                _base_document(sections=[_section(modality="table", body="这里没有表格。")]),
            )
        )


def test_rejects_figure_section_without_asset_path(tmp_path: Path) -> None:
    """L2 多模态靠 asset_path 指向图像、靠正文做文字代理检索, 缺一个就只剩空气."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(
            _write(tmp_path / "a.yaml", _base_document(sections=[_section(modality="figure")]))
        )


def test_rejects_synthetic_origin_without_flag(tmp_path: Path) -> None:
    """来源与合成标记必须自洽, 防止真实企业制度被悄悄写进演示语料."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(_write(tmp_path / "a.yaml", _base_document(synthetic_data=False)))


def test_raises_load_error_for_missing_file(tmp_path: Path) -> None:
    """缺失文件要抛配置错误而不是 FileNotFoundError, 让入库命令能统一报告原因."""

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(tmp_path / "missing.yaml")


def test_rejects_non_mapping_document(tmp_path: Path) -> None:
    """顶层写成列表会让字段校验整体跳过, 必须在进入模型之前挡掉."""

    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(["a", "b"], allow_unicode=True), encoding="utf-8")

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_document(path)


def test_rejects_duplicate_document_keys_in_catalog(tmp_path: Path) -> None:
    """两份文件声明同一个 document_key 会让幂等入库撞上唯一约束, 应在加载期暴露."""

    catalog_path = _catalog_dir(
        tmp_path, _base_document(), _base_document(title="标题不同但键相同")
    )

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_catalog(catalog_path)


def test_rejects_dangling_supersedes_reference(tmp_path: Path) -> None:
    """版本血缘指向不存在的文档, 说明前身被删或键写错, 生效期过滤会失去判据."""

    catalog_path = _catalog_dir(tmp_path, _base_document(supersedes="demo_missing_policy_v0"))

    with pytest.raises(KnowledgeLoadError):
        load_knowledge_catalog(catalog_path)


def test_catalog_accepts_first_version_without_lineage(tmp_path: Path) -> None:
    """目录校验负责两件事: 键唯一与血缘可解析; 单文档的 supersedes 为空合法."""

    catalog = load_knowledge_catalog(_catalog_dir(tmp_path, _base_document()))

    assert list(catalog) == ["demo_supplier_admission_policy_v1"]


def test_catalog_accepts_resolved_supersedes_pair(tmp_path: Path) -> None:
    """版本对是语料库的既定形状: 后继指向已存在的前身时必须解析成功."""

    expired = _base_document()
    current = _base_document(
        document_key="demo_supplier_admission_policy_v2",
        doc_version="2.0",
        supersedes="demo_supplier_admission_policy_v1",
        effective_from=date(2026, 1, 1),
        effective_until=None,
    )

    catalog = load_knowledge_catalog(_catalog_dir(tmp_path, expired, current))

    assert len(catalog) == 2
    assert catalog["demo_supplier_admission_policy_v2"].supersedes == (
        "demo_supplier_admission_policy_v1"
    )
