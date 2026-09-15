"""生成现行制度的检索片段清单 data/retrieval/chunks_v1.jsonl.

清单是从冻结语料确定性算出的派生物: 重复运行必须得到同一份文件, 因此脚本只调用
retrieval.chunk_list 的构建与写出接口, 不在这里做任何筛选或改写.

用法: uv run python scripts/build_chunk_list.py
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from vendorguard.retrieval.chunk_list import (
    CURRENT_EDITION_KEYS,
    build_chunk_records,
    write_chunk_list,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
CHUNK_LIST_PATH = REPO_ROOT / "data" / "retrieval" / "chunks_v1.jsonl"


def main() -> int:
    """构建片段清单, 落盘并打印逐版本统计, 便于人工核对现行范围."""

    records = build_chunk_records(KNOWLEDGE_DIR)
    write_chunk_list(CHUNK_LIST_PATH, records)

    counts = Counter(record.edition_key for record in records)
    print(f"候选范围: {', '.join(CURRENT_EDITION_KEYS)}")
    for edition_key, count in counts.items():
        print(f"  {edition_key}: {count} 条")
    print(f"写成 {CHUNK_LIST_PATH.relative_to(REPO_ROOT)}: 共 {len(records)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
