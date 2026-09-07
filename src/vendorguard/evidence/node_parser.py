"""将规范正文投影为可精确回放的结构化知识节点。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from hashlib import sha256

from vendorguard.evidence.schema import KnowledgeNode, NodeType

_CHINESE_NUMERAL = "零\u3007一二三四五六七八九十百千万"
_ARTICLE_PATTERN = re.compile(rf"^(第[{_CHINESE_NUMERAL}0-9]+条)(?:[ \t]+(.*))?$")
_CHAPTER_PATTERN = re.compile(rf"^(第[{_CHINESE_NUMERAL}0-9]+章(?:[ \t]+.+)?)$")
_ITEM_PATTERN = re.compile(
    rf"^(?P<label>[\uff08(][{_CHINESE_NUMERAL}0-9]+[\uff09)]|[{_CHINESE_NUMERAL}0-9]+、)"
)
_MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_SECTION_PATTERN = re.compile(rf"^(第[{_CHINESE_NUMERAL}0-9]+节(?:[ \t]+.+)?)$")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+$")


def _line_parts(text: str) -> Iterable[tuple[int, str]]:
    """逐行返回正文中每行起点和不含换行符的内容。"""

    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        yield offset, line
        offset += len(raw_line)

    if text and not text.endswith(("\n", "\r")):
        return


def _is_table_separator(line: str) -> bool:
    """判断一行是否为 Markdown 表格的列分隔行。"""

    return _TABLE_SEPARATOR_PATTERN.fullmatch(line) is not None


def _node_key(edition_key: str, node_type: NodeType, ordinal: int) -> str:
    """用版本键、节点类型和顺序生成稳定且符合 Schema 的节点键。"""

    return f"{edition_key}_{node_type}_{ordinal:04d}"


def parse_knowledge_nodes(
    *,
    edition_key: str,
    snapshot_sha256: str,
    normalized_sha256: str,
    text: str,
) -> tuple[KnowledgeNode, ...]:
    """按受控正文结构生成彼此不重叠的可引用节点。

    标题和条号只占自身字符区间. 正文, 表头和表格数据行分别形成独立节点.
    这一约束使任何节点的 ``body`` 都能直接通过 ``text[char_start:char_end]`` 回放,
    而不会出现父节点覆盖子节点正文的歧义.

    本函数只处理单份已归一化且已验证哈希的正文. 它不判断 ``edition_key`` 是否在
    语料清单中, 也不校验父节点是否已入库; 这些跨对象约束由入库层负责.
    """

    nodes: list[KnowledgeNode] = []
    node_ordinals: dict[NodeType, int] = {}
    markdown_titles: dict[int, str] = {}
    markdown_parents: dict[int, str] = {}
    current_chapter: tuple[str, str] | None = None
    current_section: tuple[str, str] | None = None
    current_article: tuple[str, str] | None = None
    table_counts: dict[tuple[str, ...], int] = {}
    pending_start: int | None = None
    pending_end: int | None = None

    def add_node(
        node_type: NodeType,
        locator: tuple[str, ...],
        char_start: int,
        char_end: int,
        parent_node_key: str | None = None,
    ) -> KnowledgeNode:
        """从正文切片构造节点, 不接受调用方提供的可伪造正文。"""

        ordinal = node_ordinals.get(node_type, 0) + 1
        node_ordinals[node_type] = ordinal
        body = text[char_start:char_end]
        node = KnowledgeNode(
            node_key=_node_key(edition_key, node_type, ordinal),
            edition_key=edition_key,
            parent_node_key=parent_node_key,
            node_type=node_type,
            locator=locator,
            snapshot_sha256=snapshot_sha256,
            normalized_sha256=normalized_sha256,
            char_start=char_start,
            char_end=char_end,
            body=body,
            body_sha256=sha256(body.encode("utf-8")).hexdigest(),
        )
        nodes.append(node)
        return node

    def current_context() -> tuple[tuple[str, ...], str | None]:
        """返回当前最精确标题路径及其对应的直接父节点。"""

        if current_article is not None:
            return tuple(current_article[0].split("\x1f")), current_article[1]
        if current_section is not None:
            return tuple(current_section[0].split("\x1f")), current_section[1]
        if current_chapter is not None:
            return tuple(current_chapter[0].split("\x1f")), current_chapter[1]
        if markdown_titles:
            level = max(markdown_titles)
            titles = tuple(markdown_titles[index] for index in sorted(markdown_titles))
            return titles, markdown_parents[level]
        return ("正文",), None

    def structural_context() -> tuple[tuple[str, ...], str | None]:
        """返回章、节或 Markdown 标题路径, 供新条文挂接。"""

        if current_section is not None:
            return tuple(current_section[0].split("\x1f")), current_section[1]
        if current_chapter is not None:
            return tuple(current_chapter[0].split("\x1f")), current_chapter[1]
        if markdown_titles:
            level = max(markdown_titles)
            titles = tuple(markdown_titles[index] for index in sorted(markdown_titles))
            return titles, markdown_parents[level]
        return (), None

    def flush_pending() -> None:
        """将连续普通文本作为一个段落节点落盘到返回序列。"""

        nonlocal pending_start, pending_end
        if pending_start is None or pending_end is None:
            return

        locator, parent_node_key = current_context()
        add_node(
            "paragraph",
            (*locator, "正文"),
            pending_start,
            pending_end,
            parent_node_key,
        )
        pending_start = None
        pending_end = None

    lines = list(_line_parts(text))
    index = 0
    while index < len(lines):
        line_start, line = lines[index]

        if not line.strip():
            flush_pending()
            index += 1
            continue

        if line.startswith("|") and index + 2 < len(lines):
            _, separator = lines[index + 1]
            if separator.startswith("|") and _is_table_separator(separator):
                flush_pending()
                context, parent_node_key = current_context()
                table_number = table_counts.get(context, 0) + 1
                table_counts[context] = table_number
                table_label = "表格" if table_number == 1 else f"表格 {table_number}"
                table = add_node(
                    "table",
                    (*context, table_label),
                    line_start,
                    line_start + len(line),
                    parent_node_key,
                )
                index += 2
                while index < len(lines) and lines[index][1].startswith("|"):
                    row_start, row = lines[index]
                    if not _is_table_separator(row):
                        add_node(
                            "table_row",
                            (*context, table_label, f"第{index + 1}行"),
                            row_start,
                            row_start + len(row),
                            table.node_key,
                        )
                    index += 1
                continue

        markdown_match = _MARKDOWN_HEADING_PATTERN.fullmatch(line)
        if markdown_match is not None:
            flush_pending()
            level = len(markdown_match.group(1))
            title = markdown_match.group(2)
            title_start = line_start + markdown_match.start(2)
            for previous_level in tuple(markdown_titles):
                if previous_level >= level:
                    del markdown_titles[previous_level]
                    del markdown_parents[previous_level]
            markdown_titles[level] = title
            parent_level = max(markdown_parents, default=0)
            parent_node_key = markdown_parents.get(parent_level)
            heading_type: NodeType = "chapter" if level == 1 else "section"
            heading = add_node(
                heading_type,
                tuple(markdown_titles[key] for key in sorted(markdown_titles)),
                title_start,
                title_start + len(title),
                parent_node_key,
            )
            markdown_parents[level] = heading.node_key
            current_chapter = None
            current_section = None
            current_article = None
            index += 1
            continue

        chapter_match = _CHAPTER_PATTERN.fullmatch(line)
        if chapter_match is not None:
            flush_pending()
            chapter = add_node("chapter", (line,), line_start, line_start + len(line))
            current_chapter = (line, chapter.node_key)
            current_section = None
            current_article = None
            markdown_titles.clear()
            markdown_parents.clear()
            index += 1
            continue

        section_match = _SECTION_PATTERN.fullmatch(line)
        if section_match is not None:
            flush_pending()
            chapter_locator = (current_chapter[0],) if current_chapter is not None else ()
            section = add_node(
                "section",
                (*chapter_locator, line),
                line_start,
                line_start + len(line),
                current_chapter[1] if current_chapter is not None else None,
            )
            current_section = ("\x1f".join((*chapter_locator, line)), section.node_key)
            current_article = None
            markdown_titles.clear()
            markdown_parents.clear()
            index += 1
            continue

        article_match = _ARTICLE_PATTERN.fullmatch(line)
        if article_match is not None:
            flush_pending()
            marker = article_match.group(1)
            context, parent_node_key = structural_context()
            article = add_node(
                "article",
                (*context, marker),
                line_start,
                line_start + len(marker),
                parent_node_key,
            )
            article_locator = (*context, marker)
            current_article = ("\x1f".join(article_locator), article.node_key)
            inline_body = article_match.group(2)
            if inline_body:
                body_start = line_start + article_match.start(2)
                add_node(
                    "paragraph",
                    (*article_locator, "正文"),
                    body_start,
                    body_start + len(inline_body),
                    article.node_key,
                )
            index += 1
            continue

        item_match = _ITEM_PATTERN.match(line)
        if item_match is not None:
            flush_pending()
            context, parent_node_key = current_context()
            add_node(
                "item",
                (*context, item_match.group("label")),
                line_start,
                line_start + len(line),
                parent_node_key,
            )
            index += 1
            continue

        if pending_start is None:
            pending_start = line_start
        pending_end = line_start + len(line)
        index += 1

    flush_pending()
    return tuple(nodes)


__all__ = ["parse_knowledge_nodes"]
