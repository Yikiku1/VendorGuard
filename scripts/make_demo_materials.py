"""生成 M2 演示用的自制营业执照 PDF 样例.

样例为固定版式的教学材料, **不是真实证照**, 也不对应任何真实企业。
脚本随仓库提交, 使样例可复现、来源可复核。

用法: ./.venv/Scripts/python.exe scripts/make_demo_materials.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

# 内置中文 CID 字体, 无需随仓库附带 TTF 文件
_FONT = "STSong-Light"
# 演示基准日期, 与 PRD、案例 YAML 及检索评测集一致
_PAGE_WIDTH, _PAGE_HEIGHT = A4


def _write_lines(
    path: Path,
    lines: list[str],
    *,
    with_checklist: bool,
    checklist_state: str = "已齐全",
) -> None:
    """把若干行文本写成一页 PDF。

    checklist_state 用一句明确的文字说明清单状态, 而不只靠勾选框:
    只有勾选符号时"清单是否齐全"是模型的推断, 加上状态行之后它是可读事实,
    与 M2 方案"布尔判断只能核对来源页码存在"的限制保持一致的诚实度。
    """

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    pdf = canvas.Canvas(str(path), pagesize=A4)
    text = pdf.beginText(60, _PAGE_HEIGHT - 80)
    text.setFont(_FONT, 12)
    text.setLeading(20)
    for line in lines:
        text.textLine(line)
    pdf.drawText(text)

    if with_checklist:
        pdf.setFont(_FONT, 12)
        pdf.drawString(60, 160, "材料清单:")
        for offset, label in enumerate(("营业执照", "法定代表人身份证明", "授权委托书")):
            y = 136 - offset * 22
            pdf.rect(60, y - 2, 11, 11, stroke=1, fill=0)
            pdf.drawString(80, y, f"\u2611 {label}")
        pdf.drawString(60, 60, f"清单状态: {checklist_state}")

    pdf.showPage()
    pdf.save()


def _write_scanned(path: Path) -> None:
    """生成一份纯图片页的样例, 用于验证扫描件被确定性拒绝。

    页面只有图片对象、没有可提取文本, 因此不需要 OCR 也能判定"不支持扫描件"。
    """

    from PIL import Image
    from reportlab.lib.utils import ImageReader

    pdf = canvas.Canvas(str(path), pagesize=A4)
    # 造一张带文字的位图并整页贴上, 使其没有文本层
    image = Image.new("RGB", (600, 200), (255, 255, 255))
    pdf.drawImage(ImageReader(image), 60, 600, width=400, height=130)
    pdf.showPage()
    pdf.save()


def main() -> None:
    target = Path("data/demo/materials")
    target.mkdir(parents=True, exist_ok=True)

    common_head = [
        "营业执照(演示样例, 非真实证照)",
        "统一社会信用代码: 91310000MA1DEMO001",
        "企业名称: 演示供应商有限公司",
        "法定代表人: 张演示",
        "登记机关: 演示市场监督管理局",
    ]

    _write_lines(
        target / "license_normal.pdf",
        [*common_head, "声明有效期至: 2027-08-31"],
        with_checklist=True,
    )

    _write_lines(
        target / "license_expired.pdf",
        [*common_head, "声明有效期至: 2026-03-31"],
        with_checklist=True,
    )

    # 缺声明有效期: 整行不出现, 用于验证"缺字段必须追问"而不是猜
    _write_lines(
        target / "license_missing_date.pdf",
        [*common_head],
        with_checklist=True,
    )

    _write_scanned(target / "license_scanned.pdf")

    for name in (
        "license_normal.pdf",
        "license_expired.pdf",
        "license_missing_date.pdf",
        "license_scanned.pdf",
    ):
        item = target / name
        print(f"{item}  {item.stat().st_size} bytes")


if __name__ == "__main__":
    main()
