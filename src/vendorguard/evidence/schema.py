"""定义并加载版本化知识库语料。"""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
NodeType = Literal[
    "document",
    "chapter",
    "section",
    "article",
    "paragraph",
    "item",
    "table",
    "table_row",
]


class KnowledgeLoadError(ValueError):
    """语料文件无法读取或未通过 Schema 校验时抛出的配置错误。"""


class KnowledgeModel(BaseModel):
    """所有语料模型的公共基类, 禁止未声明的额外字段。

    这里比 vendorguard.policy.schema 更严格是有意为之: 语料里若多写一个 permission_scope
    之类的字段并被静默忽略, 就会出现"文档声明了权限规则, 检索却对所有人公开"的错觉。
    """

    model_config = ConfigDict(extra="forbid")


class KnowledgeSource(KnowledgeModel):
    """保存知识来源的稳定身份, 不承载某一版本的具体正文。

    例如"国家行政法规库"是一个来源; 同一法规的 2014 年版和 2024 年版
    则属于这个来源下不同的 KnowledgeEdition。
    """

    source_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    source_origin: SourceOrigin
    canonical_url: str | None = None
    retrieved_at: datetime | None = None

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str | None) -> str | None:
        """公开来源地址必须是可明确记录的 HTTP(S) 地址。"""

        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("来源地址必须使用 HTTP 或 HTTPS")
        return value

    @model_validator(mode="after")
    def validate_provenance(self) -> KnowledgeSource:
        """校验来源类型, 外部地址和抓取时间之间的一致性。"""

        if self.source_origin == "synthetic":
            if self.canonical_url is not None or self.retrieved_at is not None:
                raise ValueError("自制来源不得声明外部地址或抓取时间")
            return self

        if self.canonical_url is None or self.retrieved_at is None:
            raise ValueError("公开来源必须声明地址和抓取时间")

        # 统一要求时区, 避免跨机器或夏令时环境下无法准确回放抓取时间。
        if self.retrieved_at.utcoffset() is None:
            raise ValueError("公开来源的抓取时间必须包含时区")

        return self


class KnowledgeEdition(KnowledgeModel):
    """保存一份不可变, 可按案件日期判断效力的知识版本快照。

    版本一经创建不得修改。法规或制度修订时, 必须创建新版本, 并通过
    supersedes 指向被替代的历史版本, 保证旧案件仍可回放原始引用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    version: str = Field(min_length=1)
    published_on: date
    effective_from: date
    effective_until: date | None = None
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supersedes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_edition(self) -> KnowledgeEdition:
        """校验生效期和版本血缘不会产生内部矛盾。"""

        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("失效日期不能早于生效日期")

        if self.edition_key in self.supersedes:
            raise ValueError("版本不能替代自身")

        if len(self.supersedes) != len(set(self.supersedes)):
            raise ValueError("supersedes 不能包含重复版本")

        return self

    def is_effective_on(self, as_of: date) -> bool:
        """按项目既有闭区间语义判断版本在指定日期是否有效。"""

        return self.effective_from <= as_of and (
            self.effective_until is None or as_of <= self.effective_until
        )


class KnowledgeCorpusManifest(KnowledgeModel):
    """聚合知识来源与版本快照的最小语料清单。

    后续检索入库只接收已通过此模型校验的清单, 而不是分别接收
    来源和版本列表。
    """

    sources: tuple[KnowledgeSource, ...]
    editions: tuple[KnowledgeEdition, ...]

    @model_validator(mode="after")
    def validate_references(self) -> KnowledgeCorpusManifest:
        """校验来源唯一性, 版本唯一性和同源版本血缘。"""

        source_by_key = {source.source_key: source for source in self.sources}
        if len(source_by_key) != len(self.sources):
            raise ValueError("source_key 不能重复")

        edition_by_key = {edition.edition_key: edition for edition in self.editions}
        if len(edition_by_key) != len(self.editions):
            raise ValueError("edition_key 不能重复")

        for edition in self.editions:
            if edition.source_key not in source_by_key:
                raise ValueError(f"版本引用了不存在的来源: {edition.source_key}")

            for predecessor_key in edition.supersedes:
                predecessor = edition_by_key.get(predecessor_key)

                if predecessor is None:
                    raise ValueError(f"版本替代了不存在的前身: {predecessor_key}")

                if predecessor.source_key != edition.source_key:
                    raise ValueError("版本只能替代同一来源的前身")

        return self


class KnowledgeNode(KnowledgeModel):
    """保存可回放的不可变结构节点, 是最终引用的最小单位。

    `char_start` 与 `char_end` 指向 `normalized_sha256` 对应的规范正文。
    `snapshot_sha256` 则将规范正文回连至冻结的原始字节, 避免把网页标记
    的字符偏移误作法规正文的位置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    parent_node_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    node_type: NodeType
    locator: tuple[str, ...] = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    body: str = Field(min_length=1)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """定位符各层必须有可显示的结构文本。"""

        if any(not part.strip() for part in value):
            raise ValueError("定位符不能包含空层级")
        return value

    @model_validator(mode="after")
    def validate_verbatim_reference(self) -> KnowledgeNode:
        """校验层级关系, 正文区间和哈希可精确回放。"""

        if self.parent_node_key == self.node_key:
            raise ValueError("节点不能将自身设为父节点")

        if self.char_end <= self.char_start:
            raise ValueError("字符区间终点必须晚于起点")

        if self.char_end - self.char_start != len(self.body):
            raise ValueError("字符区间长度必须等于原文正文长度")

        expected_hash = sha256(self.body.encode("utf-8")).hexdigest()
        if self.body_sha256 != expected_hash:
            raise ValueError("正文哈希必须与原文正文一致")

        return self


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
    "KnowledgeCorpusManifest",
    "KnowledgeDocument",
    "KnowledgeEdition",
    "KnowledgeLoadError",
    "KnowledgeNode",
    "KnowledgeSection",
    "KnowledgeSource",
    "load_knowledge_catalog",
    "load_knowledge_document",
]
