"""一期正式 RAG 原始快照的完整性回归测试。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
MANIFEST_PATH = KNOWLEDGE_DIR / "manifests" / "corpus_v1.json"


def _manifest() -> dict[str, Any]:
    """读取一期正式清单, 不从研究阶段候选清单推导事实。"""

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _entries() -> dict[str, dict[str, Any]]:
    """返回版本键到正式清单项的映射。"""

    entries = _manifest()["entries"]
    assert isinstance(entries, list)
    return {entry["edition_key"]: entry for entry in entries}


def test_every_official_corpus_entry_has_a_hash_verified_original_snapshot() -> None:
    """正式清单中每个版本都必须回指存在且哈希可复算的原始文件。"""

    manifest = _manifest()
    entries = manifest["entries"]

    assert manifest["schema_version"] == "1.0"
    assert manifest["edition_count"] == 10
    assert isinstance(entries, list)
    assert len(entries) == manifest["edition_count"]

    for entry in entries:
        snapshot_path = KNOWLEDGE_DIR / entry["snapshot_path"]
        acquisition_path = snapshot_path.parent / "acquisition.json"

        assert snapshot_path.is_file(), f"{entry['edition_key']} 缺少原始快照"
        assert acquisition_path.is_file(), f"{entry['edition_key']} 缺少采集记录"
        assert snapshot_path.read_bytes()
        assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == entry["snapshot_sha256"]
        assert json.loads(acquisition_path.read_text(encoding="utf-8")) == entry


def test_external_and_internal_snapshots_follow_distinct_provenance_rules() -> None:
    """外部版本必须记录可追溯来源, 内部模拟制度不得伪造外部抓取事实。"""

    for entry in _entries().values():
        if entry["source_origin"] == "synthetic":
            assert entry["canonical_url"] is None
            assert entry["retrieved_at"] is None
            assert entry["http_status"] is None
            assert entry["snapshot_path"].endswith("original.md")
        else:
            assert entry["canonical_url"].startswith("https://")
            assert entry["retrieved_at"].endswith("Z")
            assert entry["http_status"] == 200
            assert entry["snapshot_path"].endswith("original.html")


def test_enterprise_information_publicity_versions_are_separate_and_contiguous() -> None:
    """2014 与 2024 版必须各自冻结, 且闭区间有效期在修订日无空档或重叠。"""

    entries = _entries()
    previous = entries["enterprise_information_publicity_regulation_v2014"]
    current = entries["enterprise_information_publicity_regulation_v2024"]

    assert previous["snapshot_sha256"] != current["snapshot_sha256"]
    assert current["supersedes"] == [previous["edition_key"]]
    assert previous["effective_until"] == "2024-04-30"
    assert current["effective_from"] == "2024-05-01"
    assert date.fromisoformat(previous["effective_until"]) < date.fromisoformat(
        current["effective_from"]
    )


def test_direct_policy_role_is_restricted_to_current_internal_instruments() -> None:
    """直接制度依据只能是现行内部制度, 外部法规只能承担背景或条件性角色。"""

    direct_policy_entries = [
        entry for entry in _entries().values() if entry["role"] == "direct_policy"
    ]

    assert {entry["edition_key"] for entry in direct_policy_entries} == {
        "demo_supplier_admission_policy_v2",
        "demo_supplier_required_documents_policy_v3",
    }
    assert all(entry["source_origin"] == "synthetic" for entry in direct_policy_entries)
