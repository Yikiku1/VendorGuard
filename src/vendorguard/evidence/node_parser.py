"""将归一化正式正文解析为可引用的 KnowledgeNode 与仅供召回的 RetrievalChunk.

设计要点 (对应 VendorGuard-RAG检索栈交接.md 的恢复顺序第 1 步):

- 引用单位是 KnowledgeNode 的原文字符区间, 每个节点满足
  body == text[char_start:char_end], body_sha256 可复算.
- 结构树允许父子包含 (条包含款, 款包含项, 章节包含表格, 表格包含数据行),
  但兄弟节点区间互不相交; 每条非空正文行必须恰好落入一个根区间或一条
  显式丢弃记录, 任何文字不得静默消失 (守恒不变量).
- 检索粒度与引用粒度分离: RetrievalChunk 只为可引用的召回叶级单位生成
  (法规条, 制度散文章节, 表格数据行); 款/项子节点, 章/节标题节点与
  文档标题节点不生成检索块, 避免同一段文字在 BM25 中被双计.
- 定位符是版本内最小唯一定位路径, 与 data/evals/rag_phase1.json 金标准
  逐字一致: 条文 ("第八条",), 制度章节 ("正常准入条件",), 表格数据行
  ("关键物料必需材料", "第2行").
- 归一化遗留的站点页脚 (版权/备案行) 不属于任何条款, 解析为显式的
  DroppedBlock, 不并入条文体, 防止污染可引用原文.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vendorguard.evidence.schema import KnowledgeNode, RetrievalChunk

# 中文数字字符集, 与 scripts/normalize_rag_corpus.py 保持同一口径.
_CN_NUM_CHARS = "一二三四五六七八九十百零\u3007"
_CN_SMALL = "一二三四五六七八九十"

# 法规结构标记锚定在块首. 全角括号与全角零号一律用转义写入, 源码不出现全角标点.
_ARTICLE_RE = re.compile(rf"^第([{_CN_NUM_CHARS}]+)条")
_CHAPTER_RE = re.compile(rf"^第([{_CN_NUM_CHARS}]+)章")
_SECTION_HEADING_RE = re.compile(rf"^第([{_CN_NUM_CHARS}]+)节")
_ITEM_RE = re.compile(rf"^[\uff08(]([{_CN_SMALL}]+)[\uff09)]")

# 页脚噪声: 归一化脚本按 <p> 块提取时混入正文容器的站点版权行.
_SITE_NOISE_RE = re.compile("版权所有|Copyright|ICP备|运维保障")

# Markdown 标题与表格行 (仅内部制度语料使用).
_HEADING_RE = re.compile(r"^(#{1,6}) (.+)$")
_TABLE_ROW_RE = re.compile(r"^\|")
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|$")

# 外部 rerank 单条 4000 token 上限要求切块预算有断言保护; 正式语料当前
# 最长叶级节点不足 900 字符, 1600 是留有一倍余量的硬上限, 超出即报错.
MAX_CHUNK_CHARS = 1600

# 表格相关定位符词表. 数据行标签必须与 rag_phase1.json 的 "第2行" 逐字一致.
_ROW_LABEL = "第{n}行"
_TABLE_LABEL = "表格"
_PARAGRAPH_LABEL = "第{cn}款"

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


class NodeParseError(ValueError):
    """归一化正文不满足受控结构契约时抛出的解析错误.

    正式语料已冻结, 任何未预期的形状都必须中断并暴露问题, 不允许静默
    跳过或猜测归类, 否则守恒不变量失去意义, 下游入库层会拿到看似完整
    实际缺行的正文.
    """


class _Block(NamedTuple):
    """按空行切出的连续非空行块, 区间端点不含块前后空白."""

    text: str
    start: int
    end: int


class EditionInput(BaseModel):
    """一次解析所需的版本身份与归一化正文.

    normalized_sha256 必须能在内存中复算, 防止解析一份与冻结清单不一致
    的漂移文本; document_title 不进入节点正文, 只作为检索投影的上下文前缀.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    edition_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    document_title: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def verify_text_hash(self) -> EditionInput:
        """正文哈希对不上冻结清单时立即拒绝."""

        if sha256(self.text.encode("utf-8")).hexdigest() != self.normalized_sha256:
            raise ValueError("正文与 normalized_sha256 不一致, 拒绝解析漂移文本")
        return self


class DroppedBlock(BaseModel):
    """被判定为噪声而未成为节点的块, 显式记录供入库层做全覆盖复核."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    text: str = Field(min_length=1)
    reason: Literal["site_footer"]


class EditionParseResult(BaseModel):
    """一个版本的一次性解析产物, 节点与检索块均按正文顺序排列."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[KnowledgeNode, ...]
    chunks: tuple[RetrievalChunk, ...]
    dropped: tuple[DroppedBlock, ...]

    @model_validator(mode="after")
    def validate_cross_references(self) -> EditionParseResult:
        """检索块必须回指本次解析产出的节点, 各类键不得重复."""

        node_keys = [node.node_key for node in self.nodes]
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("节点键重复, 解析器计数有 bug")
        key_set = set(node_keys)
        for chunk in self.chunks:
            if chunk.node_key not in key_set:
                raise ValueError(f"检索块 {chunk.chunk_key} 引用了不存在的节点")
        chunk_keys = [chunk.chunk_key for chunk in self.chunks]
        if len(chunk_keys) != len(set(chunk_keys)):
            raise ValueError("检索块键重复, 解析器计数有 bug")
        return self


# ---------------------------------------------------------------------------
# 中文数字与文本扫描工具
# ---------------------------------------------------------------------------

_CN_VALUE = {ch: idx for idx, ch in enumerate("零一二三四五六七八九")}
_CN_VALUE["\u3007"] = 0


def _chinese_to_int(value: str) -> int:
    """把法规编号使用的中文数字解析为整数, 覆盖一期语料的 1 至 100 范围."""

    if "百" in value:
        before, _, after = value.partition("百")
        hundreds = _CN_VALUE[before] if before else 1
        return hundreds * 100 + (_chinese_to_int(after) if after else 0)
    if "十" in value:
        before, _, after = value.partition("十")
        tens = _CN_VALUE[before] if before else 1
        ones = _CN_VALUE[after] if after else 0
        return tens * 10 + ones
    return _CN_VALUE[value]


def _int_to_chinese(value: int) -> str:
    """把整数写回中文数字, 用于 款 标签; 一期语料的单条款数不超过 20."""

    if not 1 <= value <= 20:
        raise NodeParseError(f"超出中文数字转换范围: {value}")
    ones = "零一二三四五六七八九"
    if value < 10:
        return ones[value]
    if value == 10:
        return "十"
    if value < 20:
        return f"十{ones[value % 10]}"
    return "二十"


def _iter_blocks(text: str) -> list[_Block]:
    """按空行切出连续非空行块, 区间精确到首行行首与末行行尾.

    端点刻意排除块前后空白: 节点 body 的每个字符都在正文中有落点,
    块间空行成为可声明的 gap, 而不是藏在某个节点尾部撑大哈希区间.
    """

    blocks: list[_Block] = []
    cursor = 0
    pending_start: int | None = None
    pending_end = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.strip():
            if pending_start is None:
                pending_start = cursor
            pending_end = cursor + len(stripped.rstrip())
        elif pending_start is not None:
            blocks.append(_Block(text[pending_start:pending_end], pending_start, pending_end))
            pending_start = None
        cursor += len(line)
    if pending_start is not None:
        blocks.append(_Block(text[pending_start:pending_end], pending_start, pending_end))
    return blocks


def _line_spans(text: str) -> list[tuple[str, int, int]]:
    """返回每行的 (去换行文本, 起点, 终点), 终点不含换行符."""

    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        spans.append((stripped, cursor, cursor + len(stripped)))
        cursor += len(line)
    return spans


def _split_table_cells(row_text: str) -> list[str]:
    """把一行 Markdown 表格拆成去空白后的单元格列表."""

    return [cell.strip() for cell in row_text.strip().strip("|").split("|")]


# ---------------------------------------------------------------------------
# 节点与检索块收集器
# ---------------------------------------------------------------------------


class _Builder:
    """在单个版本解析过程中收集节点与检索块, 并集中兑现结构不变量.

    所有节点必须经 add_node 构造: body 逐字取原文切片, 哈希现场复算,
    定位符在版本内查重, 子节点区间做包含断言. finish 时统一执行根区间
    全覆盖检查, 让"粒度由代码算"的纪律可被机器验收.
    """

    def __init__(self, edition: EditionInput) -> None:
        """保存版本身份与原文, 初始化空收集器."""

        self._edition = edition
        self._nodes: list[KnowledgeNode] = []
        self._chunks: list[RetrievalChunk] = []
        self._dropped: list[DroppedBlock] = []
        self._coverage: list[tuple[int, int]] = []
        self._locators: set[tuple[str, ...]] = set()

    def add_node(
        self,
        *,
        node_key: str,
        parent_node_key: str | None,
        node_type: NodeType,
        locator: tuple[str, ...],
        char_start: int,
        char_end: int,
        within: tuple[int, int] | None = None,
    ) -> KnowledgeNode:
        """从原文切片构造节点, 校验区间, 哈希与定位符唯一性后登记.

        within 用于子节点对父级区间的包含断言; 章标题与条文, 文档标题与
        章节之间是谱系 (parent_node_key) 而非区间包含, 因此不传 within.
        """

        if locator in self._locators:
            raise NodeParseError(f"定位符重复, 无法作为稳定引用路径: {locator}")
        if within is not None and not (within[0] <= char_start and char_end <= within[1]):
            raise NodeParseError(f"节点 {node_key} 越出声明的父级区间")
        body = self._edition.text[char_start:char_end]
        if not body.strip():
            raise NodeParseError(f"节点 {node_key} 的区间正文为空")
        try:
            node = KnowledgeNode(
                node_key=node_key,
                edition_key=self._edition.edition_key,
                parent_node_key=parent_node_key,
                node_type=node_type,
                locator=locator,
                snapshot_sha256=self._edition.snapshot_sha256,
                normalized_sha256=self._edition.normalized_sha256,
                char_start=char_start,
                char_end=char_end,
                body=body,
                body_sha256=sha256(body.encode("utf-8")).hexdigest(),
            )
        except ValidationError as exc:
            raise NodeParseError(f"节点 {node_key} 未通过 Schema 校验: {exc}") from exc
        self._locators.add(locator)
        self._nodes.append(node)
        return node

    def add_chunk(self, node: KnowledgeNode, *, search_text: str) -> None:
        """为节点生成一个原文回指的检索投影, 并执行 rerank 预算断言.

        display_text 直接复用节点 body 区间: 命中检索块后总能回到逐字
        原文; search_text 额外携带标题与章节上下文, 只服务召回.
        """

        display_text = node.body
        if len(display_text) > MAX_CHUNK_CHARS:
            raise NodeParseError(
                f"检索块超出字符预算 {MAX_CHUNK_CHARS}: "
                f"locator={node.locator} 长度={len(display_text)}"
            )
        chunk_key = f"{node.node_key}_main"
        try:
            chunk = RetrievalChunk(
                chunk_key=chunk_key,
                edition_key=self._edition.edition_key,
                node_key=node.node_key,
                snapshot_sha256=self._edition.snapshot_sha256,
                normalized_sha256=self._edition.normalized_sha256,
                node_body_sha256=node.body_sha256,
                char_start=node.char_start,
                char_end=node.char_end,
                display_text=display_text,
                search_text=search_text,
            )
        except ValidationError as exc:
            raise NodeParseError(f"检索块 {chunk_key} 未通过 Schema 校验: {exc}") from exc
        self._chunks.append(chunk)

    def add_coverage_span(self, start: int, end: int) -> None:
        """登记一个根级覆盖区间, finish 时做全覆盖检查."""

        self._coverage.append((start, end))

    def add_dropped(self, block: _Block, reason: Literal["site_footer"]) -> None:
        """登记一个显式丢弃的噪声块, 同时计入覆盖区间."""

        self._dropped.append(
            DroppedBlock(
                char_start=block.start,
                char_end=block.end,
                text=block.text,
                reason=reason,
            )
        )
        self.add_coverage_span(block.start, block.end)

    def finish(self) -> EditionParseResult:
        """执行全覆盖检查并冻结本版本解析结果."""

        self._assert_full_coverage()
        try:
            return EditionParseResult(
                nodes=tuple(self._nodes),
                chunks=tuple(self._chunks),
                dropped=tuple(self._dropped),
            )
        except ValidationError as exc:
            raise NodeParseError(f"解析结果未通过交叉校验: {exc}") from exc

    def _assert_full_coverage(self) -> None:
        """校验根区间互不相交, 且每条非空行恰好落入一个根区间或丢弃块.

        守恒不变量的机器化形态: 解析器允许区间之间存在纯空白 gap,
        但不允许任何有内容的行游离在节点与丢弃记录之外. 所有区间都按
        行对齐构造, 因此逐行判定即可, 无需处理跨行部分覆盖.
        """

        spans = sorted(self._coverage)
        previous_end = 0
        for start, end in spans:
            if start < previous_end:
                raise NodeParseError(f"根区间在 {start} 处与前一区间重叠")
            previous_end = end
        index = 0
        for line_text, line_start, line_end in _line_spans(self._edition.text):
            if not line_text.strip():
                continue
            while index < len(spans) and spans[index][1] <= line_start:
                index += 1
            if index == len(spans) or not (
                spans[index][0] <= line_start and line_end <= spans[index][1]
            ):
                raise NodeParseError(f"正文行未被任何节点或丢弃块覆盖: {line_text[:40]!r}")


class _ArticleDraft(NamedTuple):
    """一条法规条文在闭合前的累积状态, bool 标记该块是否为 (一) 式项."""

    label: str
    number: int
    blocks: list[tuple[_Block, bool]]


# ---------------------------------------------------------------------------
# 法规类正文解析
# ---------------------------------------------------------------------------


def parse_regulation_edition(edition: EditionInput) -> EditionParseResult:
    """解析法规类归一化正文: 章, 节标题行独立成节点, 条为主引用单位.

    条节点区间从 "第X条" 块起点延伸到下一条 (或章, 节标题, 文末) 之前的
    最后一个块尾, 因此天然涵盖其全部款与项; 款子节点按自然段编号, 项子
    节点挂在紧邻的前一款之下. 条号必须严格递增; 站点页脚显式丢弃;
    检索块只为条生成, search_text 前缀携带法规标题与章, 节标签.
    """

    builder = _Builder(edition)
    article: _ArticleDraft | None = None
    chapter_key: str | None = None
    chapter_label: str | None = None
    section_key: str | None = None
    section_label: str | None = None
    last_number = 0

    def flush_article() -> None:
        """闭合当前条文, 生成条, 款, 项节点与条级检索块."""

        if article is None:
            return
        start = article.blocks[0][0].start
        end = article.blocks[-1][0].end
        parent_key = section_key if section_key is not None else chapter_key
        article_node = builder.add_node(
            node_key=f"{edition.edition_key}_article_{article.number}",
            parent_node_key=parent_key,
            node_type="article",
            locator=(article.label,),
            char_start=start,
            char_end=end,
        )
        builder.add_coverage_span(start, end)
        context_parts = [edition.document_title]
        if chapter_label is not None:
            context_parts.append(chapter_label)
        if section_label is not None:
            context_parts.append(section_label)
        context = " ".join(context_parts)
        builder.add_chunk(article_node, search_text=f"{context}\n{article_node.body}")
        paragraph_index = 0
        item_index = 0
        paragraph_node: KnowledgeNode | None = None
        paragraph_label = ""
        for block, is_item in article.blocks:
            if not is_item:
                paragraph_index += 1
                item_index = 0
                paragraph_label = _PARAGRAPH_LABEL.format(cn=_int_to_chinese(paragraph_index))
                paragraph_node = builder.add_node(
                    node_key=(
                        f"{edition.edition_key}_article_{article.number}"
                        f"_paragraph_{paragraph_index}"
                    ),
                    parent_node_key=article_node.node_key,
                    node_type="paragraph",
                    locator=(article.label, paragraph_label),
                    char_start=block.start,
                    char_end=block.end,
                    within=(start, end),
                )
                continue
            numeral = _ITEM_RE.match(block.text)
            if numeral is None or paragraph_node is None:
                raise NodeParseError(f"项块无法挂接到款节点: {block.text[:40]!r}")
            item_index += 1
            builder.add_node(
                node_key=(
                    f"{edition.edition_key}_article_{article.number}"
                    f"_paragraph_{paragraph_index}_item_{item_index}"
                ),
                parent_node_key=paragraph_node.node_key,
                node_type="item",
                locator=(article.label, paragraph_label, f"({numeral.group(1)})"),
                char_start=block.start,
                char_end=block.end,
                within=(start, end),
            )

    for block in _iter_blocks(edition.text):
        if block.text.startswith("#"):
            raise NodeParseError(f"法规正文出现意外的 Markdown 标题块: {block.text[:40]!r}")
        if _SITE_NOISE_RE.search(block.text):
            builder.add_dropped(block, "site_footer")
            continue
        article_match = _ARTICLE_RE.match(block.text)
        chapter_match = _CHAPTER_RE.match(block.text)
        section_match = _SECTION_HEADING_RE.match(block.text)
        if article_match is not None:
            flush_article()
            number = _chinese_to_int(article_match.group(1))
            if number <= last_number:
                raise NodeParseError(
                    f"条号必须严格递增, 已见 {last_number}, 读到 {article_match.group(0)}"
                )
            last_number = number
            article = _ArticleDraft(
                label=article_match.group(0),
                number=number,
                blocks=[(block, False)],
            )
            continue
        if chapter_match is not None or section_match is not None:
            flush_article()
            article = None
            heading_match = chapter_match if chapter_match is not None else section_match
            assert heading_match is not None
            label = heading_match.group(0)
            number = _chinese_to_int(heading_match.group(1))
            node_type: NodeType
            parent_key: str | None
            if chapter_match is not None:
                node_type = "chapter"
                key = f"{edition.edition_key}_chapter_{number}"
                parent_key = None
            else:
                node_type = "section"
                key = f"{edition.edition_key}_heading_{number}"
                parent_key = chapter_key
            builder.add_node(
                node_key=key,
                parent_node_key=parent_key,
                node_type=node_type,
                locator=(label,),
                char_start=block.start,
                char_end=block.end,
            )
            builder.add_coverage_span(block.start, block.end)
            if chapter_match is not None:
                chapter_key, chapter_label = key, label
                section_key, section_label = None, None
            else:
                section_key, section_label = key, label
            continue
        item_match = _ITEM_RE.match(block.text)
        if item_match is not None:
            if article is None:
                raise NodeParseError(f"项块出现在任何条文之前: {block.text[:40]!r}")
            article.blocks.append((block, True))
            continue
        if article is None:
            raise NodeParseError(f"无归属的散文块: 当前没有进行中的条文 {block.text[:40]!r}")
        article.blocks.append((block, False))
    flush_article()
    return builder.finish()


# ---------------------------------------------------------------------------
# 内部制度 Markdown 正文解析
# ---------------------------------------------------------------------------


def parse_policy_edition(edition: EditionInput) -> EditionParseResult:
    """解析内部制度类 Markdown 正文: 保留 #/## 标题层级, 表格按数据行切块.

    一个 "## 标题" 章节是一个 section 节点, 区间从标题行延伸到下一个
    #/## 标题行之前的最后一条非空行; 章节内的连续竖线行成为 table 节点,
    其数据行 (不含表头与分隔线) 逐行生成 table_row 节点, 定位符为
    (章节标题, "第N行"), 行号从 1 起按数据行计数. 一期语料只允许一级与
    二级标题, 每节至多一张表格, 破坏该形状即报错.
    """

    builder = _Builder(edition)
    lines = _line_spans(edition.text)
    first_index = next(
        (i for i, (ln, _, _) in enumerate(lines) if ln.strip()),
        None,
    )
    if first_index is None:
        raise NodeParseError("制度语料正文为空")
    title_line = lines[first_index][0]
    title_match = _HEADING_RE.match(title_line)
    if title_match is None or len(title_match.group(1)) != 1:
        raise NodeParseError(f"首行必须恰为一级标题: {title_line[:40]!r}")
    doc_start, doc_end = lines[first_index][1], lines[first_index][2]
    doc_node = builder.add_node(
        node_key=f"{edition.edition_key}_document",
        parent_node_key=None,
        node_type="document",
        locator=(title_match.group(2),),
        char_start=doc_start,
        char_end=doc_end,
    )
    builder.add_coverage_span(doc_start, doc_end)

    bounds: list[int] = []
    for i, (ln, _, _) in enumerate(lines):
        if i == first_index:
            continue
        if re.match(r"^#{3,} ", ln):
            raise NodeParseError(f"制度语料当前只允许一级与二级标题: {ln[:40]!r}")
        if ln.startswith("# "):
            raise NodeParseError(f"制度语料只允许一个一级标题: {ln[:40]!r}")
        if ln.startswith("## "):
            bounds.append(i)
    if not bounds:
        raise NodeParseError("制度语料必须包含至少一个二级标题章节")

    for position, heading_index in enumerate(bounds):
        boundary = bounds[position + 1] if position + 1 < len(bounds) else len(lines)
        heading_match = _HEADING_RE.match(lines[heading_index][0])
        assert heading_match is not None
        section_heading = heading_match.group(2)
        last_content = heading_index
        for i in range(heading_index + 1, boundary):
            if lines[i][0].strip():
                last_content = i
        section_start = lines[heading_index][1]
        section_end = lines[last_content][2]
        section_node = builder.add_node(
            node_key=f"{edition.edition_key}_section_{position + 1}",
            parent_node_key=doc_node.node_key,
            node_type="section",
            locator=(section_heading,),
            char_start=section_start,
            char_end=section_end,
        )
        builder.add_coverage_span(section_start, section_end)
        table_runs = _collect_table_runs(lines, heading_index + 1, boundary)
        if not table_runs:
            builder.add_chunk(
                section_node,
                search_text=f"{edition.document_title}\n{section_node.body}",
            )
            continue
        if len(table_runs) > 1:
            raise NodeParseError(f"章节 {section_heading} 出现多张表格, 超出冻结形状")
        _emit_table(builder, edition, section_node, section_heading, lines, table_runs[0])
    return builder.finish()


def _collect_table_runs(
    lines: list[tuple[str, int, int]],
    begin: int,
    end: int,
) -> list[list[int]]:
    """收集章节内连续的竖线行段, 每个行段是一张表格."""

    runs: list[list[int]] = []
    current: list[int] = []
    for i in range(begin, end):
        if _TABLE_ROW_RE.match(lines[i][0]):
            current.append(i)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _emit_table(
    builder: _Builder,
    edition: EditionInput,
    section_node: KnowledgeNode,
    section_heading: str,
    lines: list[tuple[str, int, int]],
    run: list[int],
) -> None:
    """把一张表格生成为 table 节点加逐数据行的 table_row 节点.

    含表格的章节不生成章节级检索块: 数据行块的 search_text 已携带文档
    标题, 章节标题与表头上下文, 再加整节投影会让同一批文字在 BM25 中
    出现两次. 表头行与分隔线只存在于 table 节点正文中, 不单独引用.
    """

    if len(run) < 3:
        raise NodeParseError(f"表格 {section_heading} 缺少表头, 分隔线或数据行")
    header_line = lines[run[0]][0]
    if not _TABLE_SEPARATOR_RE.match(lines[run[1]][0]):
        raise NodeParseError(f"表格 {section_heading} 第二行不是分隔线")
    header_cells = _split_table_cells(header_line)
    table_start = lines[run[0]][1]
    table_end = lines[run[-1]][2]
    section_span = (section_node.char_start, section_node.char_end)
    table_node = builder.add_node(
        node_key=f"{section_node.node_key}_table",
        parent_node_key=section_node.node_key,
        node_type="table",
        locator=(*section_node.locator, _TABLE_LABEL),
        char_start=table_start,
        char_end=table_end,
        within=section_span,
    )
    header_context = " ".join(header_cells)
    for row_index, line_index in enumerate(run[2:], start=1):
        row_text, row_start, row_end = lines[line_index]
        cells = _split_table_cells(row_text)
        if len(cells) != len(header_cells):
            raise NodeParseError(f"表格 {section_heading} 第{row_index}行单元格数与表头不一致")
        row_node = builder.add_node(
            node_key=f"{table_node.node_key}_row_{row_index}",
            parent_node_key=table_node.node_key,
            node_type="table_row",
            locator=(*section_node.locator, _ROW_LABEL.format(n=row_index)),
            char_start=row_start,
            char_end=row_end,
            within=(table_start, table_end),
        )
        row_context = " ".join(f"{c}:{v}" for c, v in zip(header_cells, cells, strict=True))
        builder.add_chunk(
            row_node,
            search_text=(
                f"{edition.document_title} {section_heading} {header_context}\n"
                f"{row_node.body}\n"
                f"{row_context}"
            ),
        )


# ---------------------------------------------------------------------------
# 分发与冻结语料加载
# ---------------------------------------------------------------------------


def parse_knowledge_edition(edition: EditionInput) -> EditionParseResult:
    """按正文形状分发到法规解析器或内部制度 Markdown 解析器.

    判定依据是冻结语料的两种受控形状: 制度正文以一级标题行开头, 法规
    正文以 "第一条" 开头且不含 Markdown 标记. 两种都不符合即报错, 不做
    启发式猜测.
    """

    if edition.text.lstrip().startswith("#"):
        return parse_policy_edition(edition)
    if not edition.text.lstrip().startswith("第一条"):
        raise NodeParseError(f"无法识别的正文形状: {edition.edition_key}")
    return parse_regulation_edition(edition)


def parse_frozen_corpus(knowledge_dir: Path) -> dict[str, EditionParseResult]:
    """加载并解析 data/knowledge 下的全部一期正式语料.

    读取 corpus_v1.json 与 normalized/manifest.json 两份清单, 交叉核对
    快照哈希后按清单顺序解析; 每个版本的正文哈希在 EditionInput 构造时
    复算, 因此返回结果必然绑定当前冻结字节. 该函数是解析层与冻结清单
    之间唯一的文件入口, 后续幂等入库直接复用它的输出.
    """

    corpus_payload = json.loads((knowledge_dir / "manifests" / "corpus_v1.json").read_text("utf-8"))
    normalized_payload = json.loads(
        (knowledge_dir / "normalized" / "manifest.json").read_text("utf-8")
    )
    if not isinstance(corpus_payload, dict) or not isinstance(normalized_payload, dict):
        raise NodeParseError("语料清单顶层必须是对象")
    corpus_entries = corpus_payload["entries"]
    normalized_entries = normalized_payload["entries"]
    corpus_by_key = {str(entry["edition_key"]): entry for entry in corpus_entries}
    results: dict[str, EditionParseResult] = {}
    for entry in normalized_entries:
        edition_key = str(entry["edition_key"])
        corpus_entry = corpus_by_key.get(edition_key)
        if corpus_entry is None:
            raise NodeParseError(f"归一化清单版本 {edition_key} 不在 corpus_v1.json 中")
        if entry["snapshot_sha256"] != corpus_entry["snapshot_sha256"]:
            raise NodeParseError(f"版本 {edition_key} 两份清单的快照哈希不一致")
        text = (knowledge_dir / str(entry["normalized_path"])).read_text(encoding="utf-8")
        edition = EditionInput(
            edition_key=edition_key,
            document_title=str(corpus_entry["title"]),
            snapshot_sha256=str(corpus_entry["snapshot_sha256"]),
            normalized_sha256=str(entry["normalized_sha256"]),
            text=text,
        )
        results[edition_key] = parse_knowledge_edition(edition)
    if len(results) != len(corpus_by_key):
        raise NodeParseError("归一化清单与 corpus_v1.json 版本数不一致")
    return results


__all__ = [
    "MAX_CHUNK_CHARS",
    "DroppedBlock",
    "EditionInput",
    "EditionParseResult",
    "NodeParseError",
    "parse_frozen_corpus",
    "parse_knowledge_edition",
    "parse_policy_edition",
    "parse_regulation_edition",
]
