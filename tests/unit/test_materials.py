"""材料读取的单元测试.

契约由本测试固定: materials 提供 read_text_pdf 与 MaterialDocument,
带文本层的 PDF 返回稳定材料 ID、文件指纹和逐页文本; 无法核对的材料
按可区分的失败码明确报错, 不静默返回空文本.

来源核对层由 MaterialSources 承担: 按定位符取回原文, 并核对模型声明的
日期与清单状态是否真的能在该来源里读到.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from vendorguard.materials import (
    MaterialReadError,
    MaterialSources,
    parse_declared_date,
    read_text_pdf,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MATERIALS_DIR = REPO_ROOT / "data" / "demo" / "materials"


def _write_blank_pdf(path: Path) -> None:
    """生成一份有页对象、但没有文本也没有图片的 PDF, 用于验证空白材料被拒绝."""

    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.showPage()
    pdf.save()


def test_read_text_pdf_returns_identity_digest_and_pages() -> None:
    """一页文本 PDF: 材料 ID 取文件名主干, 指纹按文件字节计算, 第 1 页含声明有效期."""

    path = MATERIALS_DIR / "license_complete.pdf"

    material = read_text_pdf(path)

    assert material.material_id == "license_complete"
    assert material.source_name == "license_complete.pdf"
    assert material.page_count == 1
    assert "2027-08-31" in material.pages[0]
    assert "演示供应商有限公司" in material.pages[0]
    assert material.sha256 == sha256(path.read_bytes()).hexdigest()


def test_read_text_pdf_keeps_page_order() -> None:
    """两页 PDF: 逐页文本各归其位, 第 1 页没有有效期, 第 2 页有清单状态."""

    material = read_text_pdf(MATERIALS_DIR / "license_missing_date.pdf")

    assert material.page_count == 2
    assert "2027-08-31" not in material.pages[0]
    assert "材料清单状态: 齐全" in material.pages[1]


def test_read_text_pdf_reports_missing_file() -> None:
    """文件不存在时给出 file_not_found, 调用方据此提示用户换一个路径."""

    with pytest.raises(MaterialReadError) as caught:
        read_text_pdf(MATERIALS_DIR / "not_here.pdf")

    assert caught.value.code == "file_not_found"


def test_read_text_pdf_rejects_non_pdf_bytes(tmp_path: Path) -> None:
    """内容不是 PDF 的字节流必须被拒绝, 不能当成没有文本层的空材料."""

    path = tmp_path / "fake.pdf"
    path.write_text("这不是 PDF", encoding="utf-8")

    with pytest.raises(MaterialReadError) as caught:
        read_text_pdf(path)

    assert caught.value.code == "not_a_pdf"


def test_read_text_pdf_rejects_blank_pdf(tmp_path: Path) -> None:
    """整份材料没有文本也没有图片时明确失败, 不静默返回空文本."""

    path = tmp_path / "blank.pdf"
    _write_blank_pdf(path)

    with pytest.raises(MaterialReadError) as caught:
        read_text_pdf(path)

    assert caught.value.code == "empty_material"


def test_read_text_pdf_detects_scanned_pdf() -> None:
    """只有图片对象、没有文本层的样例返回 scanned_pdf_unsupported."""

    with pytest.raises(MaterialReadError) as caught:
        read_text_pdf(MATERIALS_DIR / "license_scanned.pdf")

    assert caught.value.code == "scanned_pdf_unsupported"


# --- 来源核对: 定位符取回原文, 声明值必须在原文里字面命中 ---------------------


def _sources() -> MaterialSources:
    """用两份固定样例搭出来源集合; 补充轮次初始为空."""

    return MaterialSources.from_materials(
        [
            read_text_pdf(MATERIALS_DIR / "license_complete.pdf"),
            read_text_pdf(MATERIALS_DIR / "license_missing_date.pdf"),
        ]
    )


def test_sources_resolve_pages_by_locator() -> None:
    """材料定位符取回对应页原文; 页码越界、材料未登记或定位残缺都返回 None."""

    sources = _sources()
    complete_page = sources.text_for("license_complete@page:1")
    checklist_page = sources.text_for("license_missing_date@page:2")

    assert complete_page is not None
    assert "声明有效期至: 2027-08-31" in complete_page
    assert checklist_page is not None
    assert "材料清单状态: 齐全" in checklist_page
    assert sources.text_for("license_complete@page:2") is None
    assert sources.text_for("license_complete@page:0") is None
    assert sources.text_for("no_such_material@page:1") is None
    assert sources.text_for("license_complete") is None


def test_sources_resolve_only_registered_supplements() -> None:
    """只有程序登记过的补充轮次能取回原文, 没登记过的轮次返回 None."""

    sources = _sources()
    note = "补充说明: 营业执照有效期至 2027年08月31日"
    sources.add_supplement(1, note)

    assert sources.text_for("user_supplement@round:1") == note
    assert sources.text_for("user_supplement@round:2") is None
    assert sources.text_for("user_supplement@round:abc") is None


def test_sources_match_date_across_writing_styles() -> None:
    """日期比较归一化到年月日: 2027-08-31 与 2027年08月31日 等价, 差一天不认."""

    sources = _sources()
    sources.add_supplement(1, "营业执照有效期至 2027年08月31日")

    assert sources.contains_date("license_complete@page:1", date(2027, 8, 31)) is True
    assert sources.contains_date("user_supplement@round:1", date(2027, 8, 31)) is True
    assert sources.contains_date("license_complete@page:1", date(2027, 8, 30)) is False
    assert sources.contains_date("license_complete@page:2", date(2027, 8, 31)) is False


def test_sources_checklist_needs_the_explicit_literal() -> None:
    """清单布尔值必须由明确文字支撑: 齐全与不齐全互不通用, 明细行不算证据."""

    sources = _sources()
    license_page = "license_missing_date@page:1"
    checklist_page = "license_missing_date@page:2"

    assert sources.contains_checklist_complete(checklist_page, complete=True) is True
    assert sources.contains_checklist_complete(checklist_page, complete=False) is False
    assert sources.contains_checklist_complete(license_page, complete=True) is False


def test_parse_declared_date_normalizes_fixed_formats() -> None:
    """模型声明的日期只接受固定格式, 一律归一成 date 再参与核对."""

    assert parse_declared_date("2027-08-31") == date(2027, 8, 31)
    assert parse_declared_date("2027年8月31日") == date(2027, 8, 31)
    assert parse_declared_date(" 2027/08/31 ") == date(2027, 8, 31)


@pytest.mark.parametrize("raw", ["", "2027年8月", "下个月", "2027-13-01", "2027-02-30"])
def test_parse_declared_date_rejects_other_shapes(raw: str) -> None:
    """残缺、含糊或现实中不存在的日期一律拒绝, 不替模型猜一个日期出来."""

    with pytest.raises(ValueError, match="日期"):
        parse_declared_date(raw)
