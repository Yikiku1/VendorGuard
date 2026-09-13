"""生成 M2 演示用的自制材料 PDF 样例.

三份样例都是固定版式的教学材料, 不是真实证照, 也不对应任何真实企业.
脚本只负责让样例可复现: 需要重新生成时运行它, 生成结果按二进制提交进仓库.

用法: uv run python scripts/make_demo_materials.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

# 内置中文 CID 字体: 不必在仓库里附带 TTF 文件, 且 pypdf 提取回来仍是可读文本.
_FONT_NAME = "STSong-Light"
_FONT_SIZE = 12
_PAGE_SIZE = A4
_LEFT_MARGIN = 60
_TOP_MARGIN = 80
_LINE_HEIGHT = 20
_TARGET_DIR = Path("data/demo/materials")

# 声明有效期要晚于 data/demo/cases/*.yaml 的 reference_date (2026-09-01),
# 因此"完整"样例取 2027-08-31, 按规则边界在参考日期当天仍算有效.
_DEMO_REGISTRY = "演示市场监督管理局"
_COMPLETE_SUPPLIER = ("91310000MA1DEMO001", "演示供应商有限公司", "张演示")
_MISSING_DATE_SUPPLIER = ("91310000MA1DEMO002", "演示宏远部件有限公司", "李演示")

# 清单状态的写法是程序核对布尔值的唯一依据: 不从勾选框外观推断,
# 也不允许模型只凭"页码存在"宣布清单齐全.
_CHECKLIST_COMPLETE = "材料清单状态: 齐全"


def _write_lines(path: Path, pages: list[list[str]]) -> None:
    """把若干页文本写成一份 PDF, 每页一个 page 对象、每行一句 textLine.

    固定版式是刻意的: 行位置稳定, 提取回来的文本顺序才与版面一致.
    """

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT_NAME))
    pdf = canvas.Canvas(str(path), pagesize=_PAGE_SIZE)
    for lines in pages:
        text = pdf.beginText(_LEFT_MARGIN, _PAGE_SIZE[1] - _TOP_MARGIN)
        text.setFont(_FONT_NAME, _FONT_SIZE)
        text.setLeading(_LINE_HEIGHT)
        for line in lines:
            text.textLine(line)
        pdf.drawText(text)
        pdf.showPage()
    pdf.save()


def _write_scanned(path: Path) -> None:
    """生成一份纯图片页样例: 页面上只有图片对象, 没有任何可提取文本.

    这样扫描件可以被确定性识别 (无可提取文本 + 存在图片对象), 不需要 OCR,
    也不会静默返回空文本让模型去猜.
    """

    pdf = canvas.Canvas(str(path), pagesize=_PAGE_SIZE)
    block = Image.new("RGB", (600, 200), (232, 232, 232))
    pdf.drawImage(ImageReader(block), _LEFT_MARGIN, 600, width=400, height=130)
    pdf.showPage()
    pdf.save()


def _license_head(registration_id: str, company: str, legal_person: str) -> list[str]:
    """营业执照主体信息: 两份文本样例共用的抬头."""

    return [
        "营业执照(演示样例, 非真实证照)",
        f"统一社会信用代码: {registration_id}",
        f"企业名称: {company}",
        f"法定代表人: {legal_person}",
        f"登记机关: {_DEMO_REGISTRY}",
    ]


def _checklist_lines() -> list[str]:
    """材料清单段落: 明细行只是给人看的, 机器只核对最后一行状态文字."""

    return [
        "材料清单:",
        "  营业执照",
        "  法定代表人身份证明",
        "  授权委托书",
        _CHECKLIST_COMPLETE,
    ]


def _write_complete(path: Path) -> None:
    """完整样例: 一页里同时有声明有效期和明确的清单状态."""

    _write_lines(
        path,
        [
            [
                *_license_head(*_COMPLETE_SUPPLIER),
                "声明有效期至: 2027-08-31",
                "",
                *_checklist_lines(),
            ]
        ],
    )


def _write_missing_date(path: Path) -> None:
    """缺日期样例: 第 1 页没有声明有效期, 第 2 页清单状态齐全.

    缺失是"整行不出现", 而不是留一个空值: 模型没有任何可读的日期时
    只能通过 ask_user 追问, 不能自己编一个.
    """

    _write_lines(
        path,
        [
            _license_head(*_MISSING_DATE_SUPPLIER),
            _checklist_lines(),
        ],
    )


def main() -> None:
    """生成三份固定样例并打印大小, 便于人工核对生成结果."""

    _TARGET_DIR.mkdir(parents=True, exist_ok=True)

    builders = {
        "license_complete.pdf": _write_complete,
        "license_missing_date.pdf": _write_missing_date,
        "license_scanned.pdf": _write_scanned,
    }
    for name, build in builders.items():
        item = _TARGET_DIR / name
        build(item)
        print(f"{item}  {item.stat().st_size} bytes")


if __name__ == "__main__":
    main()
