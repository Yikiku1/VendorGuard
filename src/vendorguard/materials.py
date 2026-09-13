"""读取供应商材料 PDF, 提供可核对的逐页文本与来源核对.

本模块负责把 PDF 变成程序可复核的文本 (稳定材料 ID、文件指纹、逐页正文),
并按定位符回答"这个值是否真出现在所声明的来源里". 不访问数据库, 不做 OCR,
也不解析版面.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PageObject, PdfReader
from pypdf.errors import PdfReadError

# 失败码单独成类型, 调用方按码分流; 不要去解析错误消息的措辞.
MaterialFailureCode = Literal[
    "file_not_found",
    "not_a_pdf",
    "scanned_pdf_unsupported",
    "empty_material",
]

# 日期的年/月/日数字组: 同时用于解析模型声明和扫描来源原文, 因此
# 2027-08-31 与 2027年08月31日 会被归一成同一组数字再比较.
_DATE_GROUPS = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
# 清单状态的整句文字是布尔值的唯一依据; 带上"材料清单状态: "前缀,
# 才能避免"不齐全"被当成"齐全"命中.
_CHECKLIST_LITERALS = {True: "材料清单状态: 齐全", False: "材料清单状态: 不齐全"}
# 用户补充的文档 ID: 轮次由程序登记, 模型只能引用已登记的轮次.
_SUPPLEMENT_DOCUMENT_ID = "user_supplement"


class MaterialReadError(ValueError):
    """材料无法读取或无法核对时抛出的错误, 带可区分的失败码.

    继承 ValueError, 与 M1 的工具参数错误同属"输入不合法": 调用方一律
    按可恢复的输入问题处理, 不需要区分异常类型.
    """

    def __init__(self, code: MaterialFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MaterialDocument(BaseModel):
    """一份已登记的 PDF 材料: 稳定 ID、文件指纹与逐页文本.

    frozen=True 让读出来的材料在一次会话里不可改写: 页文本与指纹一旦
    读出就是这个值. 页码从 1 开始, 因此 pages[0] 是第 1 页.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    material_id: str
    source_name: str
    sha256: str
    page_count: int
    pages: tuple[str, ...]


def _parse_position(position: str, name: str) -> int | None:
    """解析定位符里的位置段 (page:2 / round:1), 形状不对返回 None."""

    prefix, separator, raw_number = position.partition(":")
    if not separator or prefix != name or not raw_number.isdigit():
        return None
    return int(raw_number)


def parse_declared_date(raw: str) -> date:
    """把模型声明的日期归一为 date, 只接受固定格式.

    允许 2027-08-31 / 2027年8月31日 / 2027/08/31 这类写法; 残缺或含糊的
    写法 (如 "2027年8月") 直接拒绝, 不替模型补全成某一天.
    """

    match = _DATE_GROUPS.fullmatch(raw.strip())
    if match is None:
        raise ValueError(f"日期格式不受支持, 需要 YYYY-MM-DD 或 YYYY年M月D日: {raw!r}")
    year, month, day = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"日期不存在: {raw!r}") from exc


def _count_image_objects(page: PageObject) -> int:
    """统计一页上的图片对象数量.

    扫描件与真正空白页的区别就在这个计数: 两者都没有可提取文本, 但
    扫描件页面里一定有图片对象, 空白页没有.
    """

    resources = page.get("/Resources")
    if resources is None:
        return 0
    xobjects = resources.get_object().get("/XObject")
    if xobjects is None:
        return 0
    return sum(
        1
        for key in xobjects.get_object()
        if xobjects.get_object()[key].get_object().get("/Subtype") == "/Image"
    )


def read_text_pdf(path: str | Path) -> MaterialDocument:
    """读取一份带文本层的 PDF, 返回稳定材料 ID、文件指纹和逐页文本.

    material_id 由程序按文件名主干生成, 不来自模型: 它决定来源定位里的
    文档 ID, 必须由程序掌握, 模型只能引用 (M2 方案 3.1). sha256 按文件
    字节计算, 用于在运行记录里标明"这次读的到底是哪份文件".

    无法核对的材料一律显式失败并带失败码: 文件不存在为 file_not_found,
    解析不了为 not_a_pdf, 整份材料没有文本层时按有无图片对象分成
    scanned_pdf_unsupported 与 empty_material. 任何情况下都不返回空文本.
    """

    material_path = Path(path)
    if not material_path.is_file():
        raise MaterialReadError("file_not_found", f"材料文件不存在: {material_path}")

    payload = material_path.read_bytes()

    try:
        reader = PdfReader(material_path)
        page_texts: list[str] = []
        image_only_pages = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            page_texts.append(text)
            if not text.strip() and _count_image_objects(page) > 0:
                image_only_pages += 1
    except (PdfReadError, OSError, ValueError) as exc:
        # 加密 PDF 也走这里: pypdf 在真正取页时才抛解密失败.
        raise MaterialReadError("not_a_pdf", f"材料无法解析为 PDF: {material_path}") from exc

    if not any(text.strip() for text in page_texts):
        if image_only_pages > 0:
            raise MaterialReadError(
                "scanned_pdf_unsupported",
                f"材料 {material_path.name} 各页均无可提取文本且含图片对象, 暂不支持扫描件",
            )
        raise MaterialReadError("empty_material", f"材料 {material_path.name} 没有可核对的文本内容")

    return MaterialDocument(
        material_id=material_path.stem,
        source_name=material_path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
        page_count=len(page_texts),
        pages=tuple(page_texts),
    )


class MaterialSources(BaseModel):
    """一次审查里可核对的来源集合: 材料逐页文本与用户补充原文.

    两者都由程序掌握而不是模型提供, 所以定位符里的材料 ID 和补充轮次
    无法被伪造. 核对机制是"声明的值必须在所声明的来源里字面命中", 用来
    替代 M1 的快照对账: M2 的事实第一次由模型从材料里读出, 程序侧没有
    真值可比, 只能核对来源.
    """

    model_config = ConfigDict(extra="forbid")

    materials: dict[str, MaterialDocument] = Field(default_factory=dict)
    supplements: dict[int, str] = Field(default_factory=dict)

    @classmethod
    def from_materials(cls, materials: Iterable[MaterialDocument]) -> MaterialSources:
        """按材料 ID 建索引; M2 一次只提交一份材料, 同 ID 时后到的覆盖先到的."""

        return cls(materials={item.material_id: item for item in materials})

    def add_supplement(self, round_number: int, text: str) -> None:
        """登记一轮用户补充的原文; 轮次号由会话递增掌握, 不由模型提供."""

        self.supplements[round_number] = text

    def text_for(self, locator: str) -> str | None:
        """按定位符取回可核对的原文, 取不到时返回 None.

        材料定位符形如 material_id@page:2 (页码从 1 开始), 用户补充形如
        user_supplement@round:1. 未登记的材料、越界页码、未登记的轮次和
        残缺定位符都返回 None, 让调用方把"来源不存在"明确报给模型.
        """

        document_id, separator, position = locator.partition("@")
        if not separator or not document_id or not position:
            return None

        if document_id == _SUPPLEMENT_DOCUMENT_ID:
            round_number = _parse_position(position, "round")
            return None if round_number is None else self.supplements.get(round_number)

        material = self.materials.get(document_id)
        if material is None:
            return None
        page_number = _parse_position(position, "page")
        if page_number is None or page_number < 1 or page_number > material.page_count:
            return None
        return material.pages[page_number - 1]

    def contains_date(self, locator: str, value: date) -> bool:
        """核对一个日期是否真出现在所声明的来源里, 比较归一化后的年月日."""

        text = self.text_for(locator)
        if text is None:
            return False
        return any(
            (int(year), int(month), int(day)) == (value.year, value.month, value.day)
            for year, month, day in _DATE_GROUPS.findall(text)
        )

    def contains_checklist_complete(self, locator: str, *, complete: bool) -> bool:
        """核对清单布尔值是否有明确文字支撑.

        只认"材料清单状态: 齐全/不齐全"这一整句; 文档明细行里出现过的
        字样、勾选框外观或页码存在都不算证据.
        """

        text = self.text_for(locator)
        return text is not None and _CHECKLIST_LITERALS[complete] in text
