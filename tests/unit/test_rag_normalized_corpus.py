"""一期归一化 RAG 正文的规模与来源绑定测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
CORPUS_MANIFEST_PATH = KNOWLEDGE_DIR / "manifests" / "corpus_v1.json"
NORMALIZED_MANIFEST_PATH = KNOWLEDGE_DIR / "normalized" / "manifest.json"


def _read_json(path: Path) -> dict[str, Any]:
    """读取结构化语料清单。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_normalized_corpus_has_one_hash_verified_output_per_frozen_edition() -> None:
    """每个正式版本恰有一个归一化正文, 且输入与输出哈希都可复算。"""

    corpus_entries = {
        entry["edition_key"]: entry for entry in _read_json(CORPUS_MANIFEST_PATH)["entries"]
    }
    normalized_manifest = _read_json(NORMALIZED_MANIFEST_PATH)
    normalized_entries = normalized_manifest["entries"]

    assert normalized_manifest["processor_version"] == "1.0"
    assert len(normalized_entries) == len(corpus_entries) == 10

    for entry in normalized_entries:
        corpus_entry = corpus_entries[entry["edition_key"]]
        normalized_path = KNOWLEDGE_DIR / entry["normalized_path"]
        snapshot_path = KNOWLEDGE_DIR / corpus_entry["snapshot_path"]

        assert entry["processor_version"] == normalized_manifest["processor_version"]
        assert entry["snapshot_sha256"] == corpus_entry["snapshot_sha256"]
        assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == entry["snapshot_sha256"]
        assert normalized_path.is_file()

        text = normalized_path.read_text(encoding="utf-8")
        assert len(text) == entry["character_count"]
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == entry["normalized_sha256"]


def test_normalized_corpus_size_is_sufficient_for_phase_one_retrieval_evaluation() -> None:
    """一期正文以约 3.5 万字为目标, 避免退化回数千字的演示节选。"""

    entries = _read_json(NORMALIZED_MANIFEST_PATH)["entries"]
    total_characters = sum(entry["character_count"] for entry in entries)

    assert 30_000 <= total_characters <= 45_000
    assert total_characters == 33_244


def test_normalized_external_documents_preserve_article_structure() -> None:
    """外部正文保留条文结构, 供后续按条、款、项而非固定窗口解析。"""

    external_edition_keys = {
        entry["edition_key"]
        for entry in _read_json(CORPUS_MANIFEST_PATH)["entries"]
        if entry["source_origin"] == "gov_document"
    }
    normalized_entries = _read_json(NORMALIZED_MANIFEST_PATH)["entries"]

    for entry in normalized_entries:
        if entry["edition_key"] not in external_edition_keys:
            continue
        text = (KNOWLEDGE_DIR / entry["normalized_path"]).read_text(encoding="utf-8")
        assert text.startswith("第一条")
        assert "<html" not in text.lower()
        assert "<script" not in text.lower()
