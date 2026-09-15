"""M3-1 片段清单的单元测试: 两份现行制度生成 22 条可回指原文的检索片段.

这组测试钉四类事情:

- 范围: 清单恰好 22 条 (6 + 16), 只含现行两份制度; 被替代的 v1 与外部法规
  留在仓库里作负例, 不进入清单.
- 回指: 每条片段都能经 node_key 找回解析器产出的 KnowledgeNode, 正文, 区间,
  定位路径与两份哈希全部一致; chunk_key 只用于召回, 不构成第二套引用身份.
- 原文: char_start/char_end 在归一化正文里切出的就是 display_text, source_path
  指向的那份冻结正文仍然算得出 normalized_sha256.
- 落盘: 写出再加载与构建结果逐字段一致, 一行一条 JSONL, 重复构建字节稳定;
  清单缺失, 为空, 混入负例版本或坏行时一律抛错, 不返回空结果——空结果会让
  "清单坏了"伪装成"制度没有规定".

清单是代码从冻结语料算出的派生物, 不是新的权威数据, 因此断言全部对着解析器
结果与归一化正文; 语料或解析器一变, 这里与 test_knowledge_node_parser.py 同时变红.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from vendorguard.evidence.node_parser import EditionParseResult, parse_frozen_corpus
from vendorguard.evidence.schema import KnowledgeNode
from vendorguard.retrieval.chunk_list import (
    CURRENT_EDITION_KEYS,
    ChunkListError,
    ChunkRecord,
    build_chunk_records,
    load_chunk_list,
    write_chunk_list,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
NORMALIZED_MANIFEST_PATH = KNOWLEDGE_DIR / "normalized" / "manifest.json"
CORPUS_MANIFEST_PATH = KNOWLEDGE_DIR / "manifests" / "corpus_v1.json"
CHUNK_LIST_PATH = REPO_ROOT / "data" / "retrieval" / "chunks_v1.jsonl"

# 现行白名单在这里独立写一份再与模块常量对照: 实现若把旧版本或法规加进候选,
# 必须同时改测试才可能通过, 避免"实现改了范围, 测试跟着改"的自证.
EXPECTED_CURRENT_EDITIONS = (
    "demo_supplier_admission_policy_v2",
    "demo_supplier_required_documents_policy_v3",
)
EXPECTED_CHUNKS_BY_EDITION = {
    "demo_supplier_admission_policy_v2": 6,
    "demo_supplier_required_documents_policy_v3": 16,
}
EXPECTED_CHUNK_TOTAL = 22

# 同一来源的旧版本是最危险的正例: 它和现行制度正文相近, 只靠检索分数分不开.
SUPERSEDED_EDITION_KEY = "demo_supplier_admission_policy_v1"

RECORD_FIELDS = (
    "chunk_key",
    "node_key",
    "edition_key",
    "document_title",
    "locator_path",
    "display_text",
    "search_text",
    "char_start",
    "char_end",
    "source_path",
    "snapshot_sha256",
    "normalized_sha256",
)


def _read_json(path: Path) -> dict[str, object]:
    """读取结构化语料清单."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _entries(path: Path) -> list[dict[str, object]]:
    """取出语料清单的 entries 列表."""

    entries = _read_json(path)["entries"]
    assert isinstance(entries, list)
    return entries


@pytest.fixture(scope="module")
def parsed_editions() -> dict[str, EditionParseResult]:
    """解析全部冻结语料, 作为节点回指的独立来源."""

    return parse_frozen_corpus(KNOWLEDGE_DIR)


@pytest.fixture(scope="module")
def normalized_texts() -> dict[str, str]:
    """按版本读取归一化正文, 用于逐字区间回指核对."""

    return {
        str(entry["edition_key"]): (KNOWLEDGE_DIR / str(entry["normalized_path"])).read_text(
            encoding="utf-8"
        )
        for entry in _entries(NORMALIZED_MANIFEST_PATH)
    }


@pytest.fixture(scope="module")
def manifest_edition_keys() -> tuple[str, ...]:
    """冻结语料里的全部版本键, 同时含现行制度与负例."""

    return tuple(str(entry["edition_key"]) for entry in _entries(NORMALIZED_MANIFEST_PATH))


@pytest.fixture(scope="module")
def corpus_titles() -> dict[str, str]:
    """按版本取出制度标题, 用于核对清单里的 document_title 不是另写的."""

    return {
        str(entry["edition_key"]): str(entry["title"]) for entry in _entries(CORPUS_MANIFEST_PATH)
    }


@pytest.fixture(scope="module")
def records() -> tuple[ChunkRecord, ...]:
    """一次性构建现行两份制度的片段清单, 供本模块各用例共享."""

    return build_chunk_records(KNOWLEDGE_DIR)


def _node_by_key(result: EditionParseResult, node_key: str) -> KnowledgeNode:
    """按节点键取唯一节点, 命中数不为 1 即失败."""

    matches = [node for node in result.nodes if node.node_key == node_key]
    assert len(matches) == 1, f"node_key {node_key} 应唯一命中, 实际 {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# 范围: 只含现行两份制度
# ---------------------------------------------------------------------------


def test_chunk_list_covers_only_the_two_current_editions(
    records: tuple[ChunkRecord, ...],
    parsed_editions: dict[str, EditionParseResult],
) -> None:
    """清单恰好 22 条, 只含现行两份制度, 且与解析器的检索块逐条同序对应."""

    assert CURRENT_EDITION_KEYS == EXPECTED_CURRENT_EDITIONS
    assert len(records) == EXPECTED_CHUNK_TOTAL

    counts = Counter(record.edition_key for record in records)
    assert dict(counts) == EXPECTED_CHUNKS_BY_EDITION

    for edition_key, expected_count in EXPECTED_CHUNKS_BY_EDITION.items():
        edition_records = [record for record in records if record.edition_key == edition_key]
        assert len(edition_records) == expected_count
        # 顺序也要钉住: 同一版本内沿用解析器输出顺序, 重复构建才得到同一份文件.
        assert [record.chunk_key for record in edition_records] == [
            chunk.chunk_key for chunk in parsed_editions[edition_key].chunks
        ]

    assert len({record.chunk_key for record in records}) == len(records)


def test_superseded_and_external_editions_are_excluded(
    records: tuple[ChunkRecord, ...],
    manifest_edition_keys: tuple[str, ...],
) -> None:
    """旧版本制度与外部法规不写入清单: 白名单是程序侧的显式范围, 不是"清单里有什么收什么"."""

    assert SUPERSEDED_EDITION_KEY in manifest_edition_keys
    assert len(manifest_edition_keys) == 10

    excluded = tuple(
        edition_key
        for edition_key in manifest_edition_keys
        if edition_key not in EXPECTED_CURRENT_EDITIONS
    )
    assert len(excluded) == 8

    listed = {record.edition_key for record in records}
    assert listed == set(EXPECTED_CURRENT_EDITIONS)
    assert listed.isdisjoint(excluded)


# ---------------------------------------------------------------------------
# 回指: 每条片段都对得上解析器节点
# ---------------------------------------------------------------------------


def test_every_record_points_back_to_a_parser_node(
    records: tuple[ChunkRecord, ...],
    parsed_editions: dict[str, EditionParseResult],
    corpus_titles: dict[str, str],
) -> None:
    """node_key, 正文, 区间, 定位路径与两份哈希都能回到解析器节点."""

    for record in records:
        result = parsed_editions[record.edition_key]
        node = _node_by_key(result, record.node_key)

        assert record.edition_key == node.edition_key
        assert record.document_title == corpus_titles[record.edition_key]
        assert record.display_text == node.body
        assert (record.char_start, record.char_end) == (node.char_start, node.char_end)
        assert record.locator_path == tuple(node.locator)
        assert record.snapshot_sha256 == node.snapshot_sha256
        assert record.normalized_sha256 == node.normalized_sha256

        # chunk_key 只用于召回: 它必须指向同一个节点, 不能变成第二套引用身份.
        matched = [chunk for chunk in result.chunks if chunk.chunk_key == record.chunk_key]
        assert len(matched) == 1
        assert matched[0].node_key == record.node_key
        assert (matched[0].char_start, matched[0].char_end) == (
            node.char_start,
            node.char_end,
        )

    # 解析器给现行制度产出的每个检索块都在清单里, 没有静默漏条.
    expected_pairs = {
        (chunk.edition_key, chunk.chunk_key)
        for edition_key in EXPECTED_CURRENT_EDITIONS
        for chunk in parsed_editions[edition_key].chunks
    }
    assert expected_pairs == {(record.edition_key, record.chunk_key) for record in records}


# ---------------------------------------------------------------------------
# 原文: 区间与来源都能回到归一化正文
# ---------------------------------------------------------------------------


def test_char_range_points_back_to_normalized_text(
    records: tuple[ChunkRecord, ...],
    normalized_texts: dict[str, str],
) -> None:
    """char_start/char_end 在归一化正文里切出的就是 display_text."""

    for record in records:
        text = normalized_texts[record.edition_key]
        assert text[record.char_start : record.char_end] == record.display_text
        # 展示原文必须包含在检索文本里, 命中片段后总能回到逐字原文.
        assert record.display_text in record.search_text


def test_source_path_reaches_the_frozen_normalized_text(
    records: tuple[ChunkRecord, ...],
) -> None:
    """source_path 与 normalized_sha256 指向同一份冻结正文, 相对 data/knowledge/ 解析."""

    for record in records:
        source = KNOWLEDGE_DIR / record.source_path
        assert source.is_file(), f"{record.source_path} 不在语料目录内"
        text = source.read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == record.normalized_sha256


# ---------------------------------------------------------------------------
# 落盘: 写出与加载
# ---------------------------------------------------------------------------


def test_write_then_load_round_trips_every_field(
    records: tuple[ChunkRecord, ...],
    tmp_path: Path,
) -> None:
    """写出再读回必须与构建结果逐字段一致."""

    path = tmp_path / "chunks_v1.jsonl"
    write_chunk_list(path, records)

    loaded = load_chunk_list(path)
    assert [item.model_dump(mode="json") for item in loaded] == [
        item.model_dump(mode="json") for item in records
    ]


def test_chunk_list_file_is_one_record_per_line(
    records: tuple[ChunkRecord, ...],
    tmp_path: Path,
) -> None:
    """清单一行一条且字段齐全, 值不含换行, 重复构建逐字节一致."""

    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_chunk_list(first, records)
    write_chunk_list(second, records)

    # 换行固定为 LF: 清单要入库, Windows 上重复构建不得把整份文件改写.
    assert first.read_bytes() == second.read_bytes()
    assert b"\r\n" not in first.read_bytes()

    lines = first.read_text(encoding="utf-8").splitlines()
    assert len(lines) == EXPECTED_CHUNK_TOTAL
    for line in lines:
        assert tuple(json.loads(line)) == RECORD_FIELDS


def test_committed_chunk_list_matches_the_builder(records: tuple[ChunkRecord, ...]) -> None:
    """入库的清单必须与当前冻结语料重新构建的结果一致, 不允许手工漂移."""

    assert CHUNK_LIST_PATH.is_file(), "先运行 uv run python scripts/build_chunk_list.py 生成清单"

    loaded = load_chunk_list(CHUNK_LIST_PATH)
    assert [item.model_dump(mode="json") for item in loaded] == [
        item.model_dump(mode="json") for item in records
    ]


def test_load_rejects_corrupt_chunk_list(records: tuple[ChunkRecord, ...], tmp_path: Path) -> None:
    """清单缺失, 为空或含坏行时抛 ChunkListError, 不返回空结果或跳过坏行."""

    base = records[0].model_dump(mode="json")
    valid_line = json.dumps(base, ensure_ascii=False)
    cases = {
        "坏 JSON": "{",
        "多余字段": json.dumps({**base, "relevance_score": 0.9}, ensure_ascii=False),
        "缺字段": json.dumps(
            {key: value for key, value in base.items() if key != "node_key"}, ensure_ascii=False
        ),
        "区间与展示文本长度不符": json.dumps(
            {**base, "char_end": int(base["char_end"]) + 1}, ensure_ascii=False
        ),
        "展示文本不在检索文本内": json.dumps(
            {**base, "search_text": "无关上下文"}, ensure_ascii=False
        ),
        "混入负例版本": json.dumps(
            {**base, "edition_key": SUPERSEDED_EDITION_KEY}, ensure_ascii=False
        ),
        "空行": f"{valid_line}\n\n",
        "空文件": "",
    }

    for name, payload in cases.items():
        path = tmp_path / f"{name}.jsonl"
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ChunkListError, match="清单"):
            load_chunk_list(path)

    with pytest.raises(ChunkListError, match="清单"):
        load_chunk_list(tmp_path / "不存在的清单.jsonl")


# ---------------------------------------------------------------------------
# 语料漂移: 片段必须追得到冻结的那份原始材料
# ---------------------------------------------------------------------------


def _copy_knowledge_dir(tmp_path: Path) -> Path:
    """把冻结语料复制到临时目录, 供漂移用例改动, 不碰仓库里的原文件."""

    drifted = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE_DIR, drifted)
    return drifted


def test_builder_refuses_normalized_text_drift(tmp_path: Path) -> None:
    """归一化正文与清单哈希不一致时必须失败, 不能照着漂移文本产出"看起来完整"的清单."""

    drifted = _copy_knowledge_dir(tmp_path)
    target = drifted / "normalized" / "demo_supplier_admission_policy_v2.txt"
    target.write_text(target.read_text(encoding="utf-8") + "追加一行\n", encoding="utf-8")

    with pytest.raises(ChunkListError, match="哈希"):
        build_chunk_records(drifted)


def test_builder_refuses_snapshot_drift(tmp_path: Path) -> None:
    """冻结快照被改动时同样失败: 正文的哈希链要一路追到原始字节."""

    drifted = _copy_knowledge_dir(tmp_path)
    target = drifted / "snapshots" / "demo_supplier_admission_policy_v2" / "original.md"
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(ChunkListError, match="快照"):
        build_chunk_records(drifted)
