"""定义并加载版本化知识库语料。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DocumentType = Literal["policy", "procedure", "reference", "case_lesson"]
Category = Literal["supplier_admission", "procurement_support", "general_admin"]
Audience = Literal["procurement", "quality", "all"]
Modality = Literal["text", "table", "figure"]
SourceOrigin = Literal["synthetic", "gov_document", "commercial"]
Authority = Literal[
    "law",
    "administrative_regulation",
    "department_rule",
    "platform_rule",
    "internal",
]


class KnowledgeLoadError(ValueError):
    """语料文件无法读取或未通过 Schema 校验时抛出的配置错误。"""


class KnowledgeModel(BaseModel):
    """所有语料模型的公共基类, 禁止未声明的额外字段。

    这里比 vendorguard.policy.schema 更严格是有意为之: 语料里若多写一个 permission_scope
    之类的字段并被静默忽略, 就会出现"文档声明了权限规则, 检索却对所有人公开"的错觉。
    """

    model_config = ConfigDict(extra="forbid")


def _contains_markdown_table(body: str) -> bool:
    """判断正文是否含表头、分隔线与至少一行数据的 Markdown 表格。

    只数竖线开头的行, 不引入 Markdown 解析器: 语料的表格是手写受控格式, 判定越简单越不容易
    把"正文里引用了一段带竖线的话"误判成表格。要求三行是因为按行切块至少要有数据行可切。
    """

    rows = [line.strip() for line in body.splitlines() if line.strip().startswith("|")]
    if len(rows) < 3:
        return False

    return any(set(row) <= set("|-: ") and "-" in row for row in rows)


class KnowledgeSection(KnowledgeModel):
    """描述一个可被定位的语料段落, 它是切块与引用的最小单位。"""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    page: int | None = Field(default=None, ge=1)
    modality: Modality
    body: str = Field(min_length=1)
    asset_path: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> KnowledgeSection:
        """校验章节形状与切块策略、定位符和 L2 多模态前提一致。"""

        if self.modality == "table" and not _contains_markdown_table(self.body):
            raise ValueError("table 章节必须包含 Markdown 表格, 否则按行切块会静默退化")
        if self.modality == "figure" and self.asset_path is None:
            raise ValueError("figure 章节必须提供 asset_path 指向图像资产")
        if self.asset_path is not None and self.modality != "figure":
            raise ValueError("只有 figure 章节可以配置 asset_path")
        return self


class KnowledgeDocument(KnowledgeModel):
    """描述一份可版本化、生效期明确的知识库语料。"""

    schema_version: Literal["1.0"]
    document_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    doc_version: str
    supersedes: list[str] = Field(default_factory=list)
    document_type: DocumentType
    authority: Authority
    category: Category
    audience: Audience
    effective_from: date
    effective_until: date | None = None
    source_origin: SourceOrigin
    source_url: str | None = None
    retrieved_at: date | None = None
    synthetic_data: bool
    language: Literal["zh-CN"] = "zh-CN"
    honest_scope_notes: list[str] | None = None
    sections: list[KnowledgeSection] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_document(self) -> KnowledgeDocument:
        """校验生效期方向、章节键唯一性以及来源标注与出处的自洽.

        来源自洽之所以要双向检查: 只查"公开来源必须带出处"挡不住把公开法规标成自制内容来
        绕过核查, 也只查"自制不得声明合成标记为 false"挡不住自制语料编一个外部 URL 冒充可溯源。
        """

        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("生效上限不能早于生效下限")

        section_keys = [section.key for section in self.sections]
        if len(section_keys) != len(set(section_keys)):
            raise ValueError("章节键不能重复")

        if self.source_origin == "synthetic":
            if not self.synthetic_data:
                raise ValueError("source_origin 为 synthetic 时必须声明 synthetic_data 为 true")
            if self.source_url is not None or self.retrieved_at is not None:
                raise ValueError(
                    "自制语料不得声明外部出处, source_url 与 retrieved_at 仅用于公开快照"
                )
            return self

        if self.synthetic_data:
            raise ValueError("公开来源快照不能声明 synthetic_data 为 true")
        if self.source_url is None or self.retrieved_at is None:
            raise ValueError("公开来源快照必须同时提供 source_url 与 retrieved_at")
        return self


def load_knowledge_document(path: str | Path) -> KnowledgeDocument:
    """使用 UTF-8 读取并校验单份 YAML 语料。"""

    document_path = Path(path)
    try:
        raw_data = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise KnowledgeLoadError(f"无法读取知识库语料文件: {document_path}") from exc

    if not isinstance(raw_data, dict):
        raise KnowledgeLoadError(f"知识库语料顶层必须是映射: {document_path}")

    try:
        return KnowledgeDocument.model_validate(raw_data)
    except ValidationError as exc:
        raise KnowledgeLoadError(f"知识库语料未通过 Schema 校验: {document_path}") from exc


def load_knowledge_catalog(directory: str | Path) -> dict[str, KnowledgeDocument]:
    """递归加载目录下全部 YAML 语料, 并校验跨文件的键唯一性与版本血缘。

    键唯一和 supersedes 可解析只有在看到整个目录后才能判断, 因此单文档校验不含这两条。
    空目录返回空结果, 是否拒绝由调用方决定: 入库命令必须拒绝空语料, 而工具函数保持中立
    才不会把某一种业务预期焊死在加载层。
    """

    catalog: dict[str, KnowledgeDocument] = {}
    for path in sorted(Path(directory).rglob("*.y*ml")):
        document = load_knowledge_document(path)
        if document.document_key in catalog:
            raise KnowledgeLoadError(f"语料 document_key 重复: {document.document_key}")
        catalog[document.document_key] = document

    for document in catalog.values():
        for predecessor in document.supersedes:
            if predecessor == document.document_key:
                raise KnowledgeLoadError(
                    f"语料 {document.document_key} 的 supersedes 自引用, 版本链会陷入循环"
                )
            if predecessor not in catalog:
                raise KnowledgeLoadError(
                    f"语料 {document.document_key} 的 supersedes 指向不存在的文档: {predecessor}"
                )

    return catalog


__all__ = [
    "KnowledgeDocument",
    "KnowledgeLoadError",
    "KnowledgeSection",
    "load_knowledge_catalog",
    "load_knowledge_document",
]
