"""用真实缓存跑一次检索, 打印 Top-5 与重复调用的排序一致性.

M3-3 的完成判据要在命令行上看得到: 给定真实查询返回确定的 Top-5 (含节点 ID, 原文与
定位), 同一查询重复检索排序不变. 服务端向量不是逐次确定的, 所以"可复现"绑定的是
**当前这份缓存文件**, 不是模型本身.

用法: uv run python scripts/preview_search.py ["查询文本"]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

from vendorguard.config import load_settings
from vendorguard.retrieval.chunk_list import CURRENT_EDITION_KEYS, load_chunk_list
from vendorguard.retrieval.embedding import load_or_build_vectors
from vendorguard.retrieval.search import search_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
CHUNK_LIST_PATH = REPO_ROOT / "data" / "retrieval" / "chunks_v1.jsonl"
ENV_PATH = REPO_ROOT / ".env"
DEFAULT_QUERY = "关键物料品类是否可以先准入后补交质量证书"


def main() -> int:
    """加载片段清单与向量缓存, 检索两次并打印结果."""

    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY

    env = {**dotenv_values(ENV_PATH), **os.environ}
    api_key = env.get("VENDORGUARD_LLM_API_KEY")
    base_url = env.get("VENDORGUARD_LLM_BASE_URL")
    if not api_key or not base_url:
        print("缺少 VENDORGUARD_LLM_API_KEY 或 VENDORGUARD_LLM_BASE_URL, 先在 .env 里配置")
        return 2

    settings = load_settings()
    records = load_chunk_list(CHUNK_LIST_PATH)
    client = OpenAI(api_key=str(api_key), base_url=str(base_url), timeout=60.0, max_retries=0)
    vectors = load_or_build_vectors(
        records,
        client=client,
        model=settings.embedding_model,
        cache_path=settings.embedding_cache_path,
    )

    def run() -> tuple[str, ...]:
        results = search_policy(
            query,
            records=records,
            vectors=vectors,
            embedding_client=client,
            model=settings.embedding_model,
        )
        for rank, item in enumerate(results, start=1):
            print(f"\n[{rank}] score={item.score:.4f}")
            print(f"    chunk_key: {item.chunk_key}")
            print(f"    node_key:  {item.node_key}")
            print(f"    定位: {list(item.locator_path)} 区间: {item.char_start}-{item.char_end}")
            print(f"    原文: {item.display_text.splitlines()[-1][:80]}")
        return tuple(item.chunk_key for item in results)

    print(f"查询: {query}")
    print(f"候选: {len(records)} 条, 现行范围: {', '.join(CURRENT_EDITION_KEYS)}")

    first = run()
    second = run()
    print(f"\n重复检索排序一致: {first == second}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
