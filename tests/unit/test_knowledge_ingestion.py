"""冻结语料入库计划与幂等追加写入的契约测试。"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.evidence.ingestion import (
    CorpusIngestionError,
    ingest_frozen_corpus,
    load_frozen_corpus_plan,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = REPOSITORY_ROOT / "data" / "knowledge"
CORPUS_MANIFEST_PATH = KNOWLEDGE_ROOT / "manifests" / "corpus_v1.json"
NORMALIZED_MANIFEST_PATH = KNOWLEDGE_ROOT / "normalized" / "manifest.json"


class _RecordingSession:
    """记录 SQLAlchemy 写入语句, 让入库顺序不依赖本地 PostgreSQL。"""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> None:
        """保存待执行语句, 模拟 AsyncSession.execute 的最小异步边界。"""

        self.statements.append(statement)


def _load_plan():
    """加载仓库中唯一允许入库的一期冻结语料。"""

    return load_frozen_corpus_plan(
        corpus_manifest_path=CORPUS_MANIFEST_PATH,
        normalized_manifest_path=NORMALIZED_MANIFEST_PATH,
    )


def test_plan_binds_every_frozen_edition_to_verified_nodes_and_chunks() -> None:
    """正式语料计划包含全部 10 个版本, 节点和检索投影一一绑定。"""

    plan = _load_plan()
    editions = {edition.edition_key: edition for edition in plan.editions}
    node_keys = {node.node_key for node in plan.nodes}

    assert len(plan.sources) == 6
    assert len(editions) == 10
    assert len(plan.nodes) == len(plan.chunks) == 916
    assert editions["enterprise_information_publicity_regulation_v2024"].canonical_url == (
        "https://xzfg.moj.gov.cn/front/law/detail?LawID=1718"
    )
    assert all(chunk.node_key in node_keys for chunk in plan.chunks)
    assert all(chunk.display_text in chunk.search_text for chunk in plan.chunks)


def test_plan_rejects_a_normalized_manifest_with_tampered_hash(tmp_path: Path) -> None:
    """入库前重新计算正文哈希, 不信任已写入 JSON 的摘要字段。"""

    temporary_knowledge_root = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE_ROOT, temporary_knowledge_root)
    normalized_manifest_path = temporary_knowledge_root / "normalized" / "manifest.json"
    manifest = json.loads(normalized_manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["normalized_sha256"] = "0" * 64
    normalized_manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(CorpusIngestionError, match="归一化正文哈希不匹配"):
        load_frozen_corpus_plan(
            corpus_manifest_path=temporary_knowledge_root / "manifests" / "corpus_v1.json",
            normalized_manifest_path=normalized_manifest_path,
        )


@pytest.mark.asyncio
async def test_ingestion_uses_conflict_ignoring_inserts_in_dependency_order() -> None:
    """重复运行只追加缺失行, 且来源、版本、节点、投影按外键顺序写入。"""

    plan = _load_plan()
    recording_session = _RecordingSession()

    result = await ingest_frozen_corpus(cast(AsyncSession, recording_session), plan)
    table_names = [statement.table.name for statement in recording_session.statements]
    table_counts = Counter(table_names)
    compiled_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in recording_session.statements
    ]

    assert result.source_count == table_counts["knowledge_sources"] == 6
    assert result.edition_count == table_counts["knowledge_editions"] == 10
    assert result.supersession_count == table_counts["knowledge_edition_supersessions"] == 2
    assert result.node_count == table_counts["knowledge_nodes"] == 916
    assert result.chunk_count == table_counts["retrieval_chunks"] == 916
    assert table_names == sorted(
        table_names,
        key=(
            "knowledge_sources",
            "knowledge_editions",
            "knowledge_edition_supersessions",
            "knowledge_nodes",
            "retrieval_chunks",
        ).index,
    )
    assert all("ON CONFLICT DO NOTHING" in statement for statement in compiled_sql)
