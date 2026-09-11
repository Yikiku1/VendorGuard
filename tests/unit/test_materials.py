"""材料读取与来源核对的单元测试."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from vendorguard.materials import (
    MaterialReadError,
    MaterialText,
    SourceTexts,
    read_material,
)

_FONT = "STSong-Light"


def _write_pdf(path: Path, pages: list[str], *, with_image: bool = False) -> None:
    """生成一份测试用 PDF; with_image 时在页面上放一个图片对象."""

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for text in pages:
        pdf.setFont(_FONT, 12)
        pdf.drawString(60, 760, text)
        if with_image:
            # 极小的内联图, 只为在页面上产生 /Subtype /Image 对象
            pdf.drawImage(_tiny_image(), 60, 600, width=20, height=20, mask=None)
        pdf.showPage()
    pdf.save()


def _tiny_image() -> object:
    """返回一个 reportlab 可绘制的 2x2 图像对象."""

    from PIL import Image  # type: ignore[import-not-found]
    from reportlab.lib.utils import ImageReader

    image = Image.new("RGB", (2, 2), (255, 255, 255))
    return ImageReader(image)


def test_read_material_extracts_chinese_text(tmp_path: Path) -> None:
    """中文正文逐页提取无损, 页码从 1 开始且可定位."""

    path = tmp_path / "license_demo.pdf"
    _write_pdf(path, ["声明有效期至: 2027-08-31", "第二页内容"])

    material = read_material(path)

    assert material.material_id == "license_demo"
    assert material.page_count == 2
    assert "2027-08-31" in material.pages[0]
    assert "第二页内容" in material.page(2)
    assert len(material.sha256) == 64


def test_read_material_reports_missing_file(tmp_path: Path) -> None:
    """文件不存在时抛出带失败码的错误, 便于调用方分别提示."""

    with pytest.raises(MaterialReadError) as caught:
        read_material(tmp_path / "not_here.pdf")

    assert caught.value.code == "file_not_found"


def test_read_material_rejects_non_pdf(tmp_path: Path) -> None:
    """不是 PDF 的字节流必须被拒绝, 而不是当成空材料."""

    path = tmp_path / "fake.pdf"
    path.write_text("这不是 PDF", encoding="utf-8")

    with pytest.raises(MaterialReadError) as caught:
        read_material(path)

    assert caught.value.code == "not_a_pdf"


def test_read_material_detects_scanned_pdf(tmp_path: Path) -> None:
    """无可提取文本且含图片对象的页面判定为扫描件, 不静默降级为空文本."""

    path = tmp_path / "scanned.pdf"
    _write_pdf(path, ["", ""], with_image=True)

    with pytest.raises(MaterialReadError) as caught:
        read_material(path)

    assert caught.value.code == "scanned_pdf"


def test_read_material_honours_explicit_material_id(tmp_path: Path) -> None:
    """材料 ID 可由程序显式指定, 它决定来源定位里的文档 ID."""

    path = tmp_path / "whatever.pdf"
    _write_pdf(path, ["内容"])

    material = read_material(path, material_id="BL-001")

    assert material.material_id == "BL-001"


def _material() -> MaterialText:
    return MaterialText(
        material_id="BL-001",
        source_name="bl.pdf",
        sha256="a" * 64,
        page_count=2,
        pages=("声明有效期至: 2027年08月31日", "第二页无日期"),
    )


def test_sources_contains_date_accepts_chinese_format() -> None:
    """日期核对比较数字组, 因此 2027年08月31日 与 2027-08-31 等价."""

    sources = SourceTexts.from_materials([_material()])

    assert sources.contains_date("BL-001@page:1", date(2027, 8, 31)) is True
    assert sources.contains_date("BL-001@page:1", date(2027, 8, 30)) is False


def test_sources_contains_date_rejects_unknown_locator() -> None:
    """伪造页码或未知材料 ID 一律核对失败, 模型无法凭空造来源."""

    sources = SourceTexts.from_materials([_material()])

    assert sources.contains_date("BL-001@page:9", date(2027, 8, 31)) is False
    assert sources.contains_date("BL-999@page:1", date(2027, 8, 31)) is False
    assert sources.contains_date("随便写@page:1", date(2027, 8, 31)) is False


def test_sources_supplement_round_is_program_controlled() -> None:
    """用户补充按程序掌握的轮次登记; 未登记的轮次核对失败."""

    sources = SourceTexts.from_materials([_material()])
    sources.add_supplement(1, "声明有效期至 2028-12-31")

    assert sources.contains_date("用户补充@R1", date(2028, 12, 31)) is True
    assert sources.contains_date("用户补充@R2", date(2028, 12, 31)) is False


def test_sources_contains_flag_requires_existing_page() -> None:
    """布尔判断只核对来源页码存在且非空, 不宣称判断本身正确."""

    sources = SourceTexts.from_materials([_material()])

    assert sources.contains_flag("BL-001@page:1") is True
    assert sources.contains_flag("BL-001@page:9") is False


def test_source_texts_serializes_material_digest() -> None:
    """来源文本可序列化, 使运行记录能留下材料摘要供第三方复核."""

    payload = json.loads(SourceTexts.from_materials([_material()]).model_dump_json())

    assert payload["materials"]["BL-001"]["sha256"] == "a" * 64
