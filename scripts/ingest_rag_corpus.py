"""将一期冻结 RAG 语料以不可覆盖方式写入数据库。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from vendorguard.config import Settings, load_settings
from vendorguard.database import (
    create_database_engine,
    create_session_factory,
    transactional_session,
)
from vendorguard.evidence.ingestion import (
    CorpusIngestionResult,
    ingest_frozen_corpus,
    load_frozen_corpus_plan,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_ROOT = REPOSITORY_ROOT / "data" / "knowledge"


async def ingest_phase_one_corpus(settings: Settings) -> CorpusIngestionResult:
    """校验一期冻结语料, 并在单个事务内追加来源、版本、节点和检索投影。"""

    plan = load_frozen_corpus_plan(
        corpus_manifest_path=KNOWLEDGE_ROOT / "manifests" / "corpus_v1.json",
        normalized_manifest_path=KNOWLEDGE_ROOT / "normalized" / "manifest.json",
    )
    engine = create_database_engine(settings)

    try:
        session_factory = create_session_factory(engine)
        async with transactional_session(session_factory) as session:
            return await ingest_frozen_corpus(session, plan)
    finally:
        await engine.dispose()


def main() -> None:
    """运行命令并输出本次入库计划覆盖的记录数量。"""

    result = asyncio.run(ingest_phase_one_corpus(load_settings()))
    print(
        "RAG 语料入库完成: "
        f"来源 {result.source_count}, 版本 {result.edition_count}, "
        f"版本关系 {result.supersession_count}, 节点 {result.node_count}, "
        f"检索投影 {result.chunk_count}。"
    )


if __name__ == "__main__":
    main()
