"""从一期原始快照生成可检索的受控正文。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
MANIFEST_PATH = KNOWLEDGE_DIR / "manifests" / "corpus_v1.json"
NORMALIZED_DIR = KNOWLEDGE_DIR / "normalized"
PROCESSOR_VERSION = "1.0"
ARTICLE_PATTERN = re.compile(r"^第([一二三四五六七八九十百零\u3007]+)条")

# 受控区间覆盖一期的登记、信息公示、认证与质量边界, 不将无关执法细节填入检索库。
MAX_ARTICLE_BY_EDITION = {
    "market_entity_registration_regulation_v2021": 22,
    "market_entity_registration_rules_v2022": 23,
    "enterprise_information_publicity_regulation_v2014": 25,
    "enterprise_information_publicity_regulation_v2024": 27,
    "certification_and_accreditation_regulation_v2023": 77,
    "compulsory_product_certification_rules_v2022": 33,
    "product_quality_law_v2018": 39,
}
CHINESE_NUMBERS = {
    "零": 0,
    "\u3007": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "百": 100,
}


class _BlockParser(HTMLParser):
    """提取正文容器中的可见文本, 忽略脚本、样式和重复空白。"""

    content_tags: ClassVar[frozenset[str]] = frozenset(
        {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td"}
    )
    ignored_tags: ClassVar[frozenset[str]] = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._ignored_depth = 0
        self._active_tag: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self.ignored_tags:
            self._ignored_depth += 1
        if self._ignored_depth == 0 and tag in self.content_tags:
            self._active_tag = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.ignored_tags and self._ignored_depth > 0:
            self._ignored_depth -= 1
        if tag != self._active_tag:
            return
        block = re.sub(r"\s+", " ", "".join(self._parts)).strip()
        if block:
            self.blocks.append(block)
        self._active_tag = None
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active_tag is not None and self._ignored_depth == 0:
            self._parts.append(data)


def _sha256(content: bytes) -> str:
    """返回字节内容的 SHA-256 摘要。"""

    return hashlib.sha256(content).hexdigest()


def _parse_chinese_number(value: str) -> int:
    """解析法规条号使用的中文数字, 覆盖一期语料的 1 至 100 范围。"""

    if value == "十":
        return 10
    if "十" in value:
        before, _, after = value.partition("十")
        tens = CHINESE_NUMBERS[before] if before else 1
        ones = CHINESE_NUMBERS[after] if after else 0
        return tens * 10 + ones
    return CHINESE_NUMBERS[value]


def _extract_article_range(html: str, maximum_article: int) -> str:
    """保留从第一条至指定条号的连续正文及条内款项。"""

    parser = _BlockParser()
    parser.feed(html)

    selected: list[str] = []
    started = False
    for block in parser.blocks:
        match = ARTICLE_PATTERN.match(block)
        if match is not None:
            article_number = _parse_chinese_number(match.group(1))
            if article_number > maximum_article:
                break
            started = True
        if started:
            selected.append(block)

    if not selected:
        raise ValueError(f"未从原始 HTML 提取到第一条至第二十条: {maximum_article}")
    return "\n\n".join(selected) + "\n"


def _normalized_text(entry: dict[str, object]) -> str:
    """按来源类型从冻结原件生成可检索正文。"""

    snapshot_path = KNOWLEDGE_DIR / str(entry["snapshot_path"])
    content = snapshot_path.read_bytes()
    if _sha256(content) != entry["snapshot_sha256"]:
        raise ValueError(f"原始快照哈希不匹配: {entry['edition_key']}")

    if entry["source_origin"] == "synthetic":
        return content.decode("utf-8")

    maximum_article = MAX_ARTICLE_BY_EDITION[str(entry["edition_key"])]
    return _extract_article_range(content.decode("utf-8", errors="strict"), maximum_article)


def normalize_corpus() -> None:
    """一次性生成一期归一化正文与哈希清单, 禁止覆盖既有输出。"""

    if NORMALIZED_DIR.exists():
        raise FileExistsError("normalized 目录已存在, 不允许原地覆盖正式语料")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) != 10:
        raise ValueError("一期正式清单必须包含 10 个版本")

    temporary_dir = NORMALIZED_DIR.with_name("normalized.tmp")
    if temporary_dir.exists():
        raise FileExistsError("发现未清理的 normalized.tmp, 请先人工核查")
    temporary_dir.mkdir()

    metadata_entries: list[dict[str, object]] = []
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("正式清单包含无效条目")
            text = _normalized_text(entry)
            path = temporary_dir / f"{entry['edition_key']}.txt"
            path.write_text(text, encoding="utf-8")
            metadata_entries.append(
                {
                    "edition_key": entry["edition_key"],
                    "snapshot_sha256": entry["snapshot_sha256"],
                    "normalized_path": f"normalized/{path.name}",
                    "normalized_sha256": _sha256(text.encode("utf-8")),
                    "character_count": len(text),
                    "processor_version": PROCESSOR_VERSION,
                }
            )

        total_characters = sum(item["character_count"] for item in metadata_entries)
        if not 30_000 <= total_characters <= 45_000:
            raise ValueError(f"归一化正文应约 3.5 万字, 当前为 {total_characters} 字")
        (temporary_dir / "manifest.json").write_text(
            json.dumps(
                {"processor_version": PROCESSOR_VERSION, "entries": metadata_entries},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        shutil.move(str(temporary_dir), str(NORMALIZED_DIR))
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


if __name__ == "__main__":
    normalize_corpus()
