"""读取供应商材料 PDF, 提供可核对的逐页文本与扫描件判定.

本模块只做三件事: 逐页提取文本, 判定材料是否可核对, 以及按定位符
回答"这个值是否真的出现在所声明的页里"。不访问数据库, 不做 OCR,
也不对无法提取的材料做静默降级。

扫描件判定沿用 M2 方案第 5 节的实测结论: 无可提取文本且页面存在图片
对象, 即为图片型材料, 明确报错而不是返回空文本让模型去猜。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pypdf import PdfReader
from pypdf.errors import PdfReadError

# 来源定位格式与既有 StructuredFacts 一致: 材料ID@page:N
_LOCATOR_PATTERN = re.compile(r"^(?P<document>[A-Za-z0-9_\-]+)@page:(?P<page>\d+)$")
# 用户补充的来源形如 用户补充@R2, 由程序按轮次生成, 模型无法伪造
_SUPPLEMENT_PATTERN = re.compile(r"^用户补充@R(?P<round>\d+)$")
# 日期数字组: 兼容 2027-08-31 / 2027年08月31日 / 2027/08/31
_DATE_NUMBER_PATTERN = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")

MaterialFailureCode = Literal[
    "file_not_found",
    "not_a_pdf",
    "scanned_pdf",
    "empty_material",
]


class MaterialReadError(ValueError):
    """材料无法读取或无法核对时抛出的错误, 带可区分的失败码。"""

    def __init__(self, code: MaterialFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MaterialText(BaseModel):
    """一份材料的逐页提取文本, 页码从 1 开始。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    material_id: str
    source_name: str
    sha256: str
    page_count: int
    pages: tuple[str, ...]

    def page(self, number: int) -> str:
        """取第 number 页文本, 页码越界时抛出可区分的失败。"""

        if number < 1 or number > self.page_count:
            raise MaterialReadError(
                "empty_material", f"材料 {self.material_id} 不存在第 {number} 页"
            )
        return self.pages[number - 1]


def _count_image_objects(page: object) -> int:
    """统计一页上的图片 XObject 数量, 用于判定扫描件。"""

    resources = page.get("/Resources")  # type: ignore[attr-defined]
    if resources is None:
        return 0
    xobjects = resources.get_object().get("/XObject")
    if xobjects is None:
        return 0
    count = 0
    for key in xobjects.get_object():
        if xobjects.get_object()[key].get_object().get("/Subtype") == "/Image":
            count += 1
    return count


def read_material(path: str | Path, *, material_id: str | None = None) -> MaterialText:
    """读取一份 PDF 材料并逐页提取文本。

    加密、损坏与图片型材料分别抛出带失败码的 MaterialReadError, 调用方
    据此给出不同提示。material_id 缺省时取文件名主干, 作为来源定位的
    文档 ID; 它由程序决定, 模型无法自行指定。
    """

    material_path = Path(path)
    if not material_path.is_file():
        raise MaterialReadError("file_not_found", f"材料文件不存在: {material_path}")

    from hashlib import sha256 as _sha256

    payload = material_path.read_bytes()
    digest = _sha256(payload).hexdigest()
    resolved_id = material_id or material_path.stem

    try:
        reader = PdfReader(material_path)
    except (PdfReadError, OSError, ValueError) as exc:
        raise MaterialReadError("not_a_pdf", f"无法解析为 PDF: {material_path}") from exc

    if reader.is_encrypted:
        raise MaterialReadError("not_a_pdf", f"材料已加密, 无法读取: {material_path}")

    pages: list[str] = []
    image_pages = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text)
        if not text.strip() and _count_image_objects(page) > 0:
            image_pages += 1

    if not pages:
        raise MaterialReadError("empty_material", f"材料没有任何页面: {material_path}")

    if image_pages == len(pages):
        raise MaterialReadError(
            "scanned_pdf",
            f"材料 {resolved_id} 各页均无可提取文本且含图片对象, 暂不支持扫描件",
        )

    return MaterialText(
        material_id=resolved_id,
        source_name=material_path.name,
        sha256=digest,
        page_count=len(pages),
        pages=tuple(pages),
    )


class SourceTexts(BaseModel):
    """本次可核对的来源文本: 材料逐页文本与用户补充原文。

    两者都由程序掌握而非模型提供, 因此来源定位里的材料 ID 与补充轮次
    无法被伪造。核对采用"值必须在所声明的来源里字面命中"的机制, 替代
    M1 的快照对账——M2 的事实第一次来自模型读数, 程序侧没有真值可比。
    """

    model_config = ConfigDict(extra="forbid")

    materials: dict[str, MaterialText] = {}
    supplements: dict[int, str] = {}

    @classmethod
    def from_materials(cls, materials: list[MaterialText]) -> SourceTexts:
        """按材料 ID 建立索引, 供核对时按来源定位查找。"""

        return cls(materials={item.material_id: item for item in materials})

    def add_supplement(self, round_number: int, text: str) -> None:
        """登记一轮用户补充的原文, 轮次由程序递增掌握。"""

        self.supplements[round_number] = text

    def text_for(self, locator: str) -> str | None:
        """按来源定位取出可核对的原文; 定位非法或来源不存在返回 None。"""

        material_match = _LOCATOR_PATTERN.match(locator)
        if material_match is not None:
            material = self.materials.get(material_match.group("document"))
            if material is None:
                return None
            page_number = int(material_match.group("page"))
            if page_number < 1 or page_number > material.page_count:
                return None
            return material.pages[page_number - 1]

        supplement_match = _SUPPLEMENT_PATTERN.match(locator)
        if supplement_match is not None:
            return self.supplements.get(int(supplement_match.group("round")))

        return None

    def contains_date(self, locator: str, value: date) -> bool:
        """核对一个日期值是否出现在声明的来源里。

        比对年/月/日三个数字组而不是字符串相等, 因此 `2027-08-31` 与
        `2027年08月31日` 都能核对通过; 只要来源里存在同一天的书写即算命中。
        """

        text = self.text_for(locator)
        if text is None:
            return False
        for year, month, day in _DATE_NUMBER_PATTERN.findall(text):
            if (int(year), int(month), int(day)) == (value.year, value.month, value.day):
                return True
        return False

    def contains_flag(self, locator: str) -> bool:
        """核对某个布尔判断所引用的来源是否真实存在。

        布尔值(如清单是否齐全)是模型对材料内容的判断, 程序只能确认它
        引用的页码存在且非空, 无法验证判断本身正确。这条限制在文档与
        演示中必须如实说明, 不得宣称全部事实已程序验证。
        """

        text = self.text_for(locator)
        return text is not None and bool(text.strip())


__all__ = [
    "MaterialFailureCode",
    "MaterialReadError",
    "MaterialText",
    "SourceTexts",
    "read_material",
]
