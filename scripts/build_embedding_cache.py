"""生成或复用现行制度片段的正文向量缓存.

缓存是调付费接口算出的派生物: 模型名或任意一条正文变化都会让整份失效并重建,
所以这个脚本可以随时重跑. 二次运行只读缓存, 接口调用次数会打印出来, 0 次即
证明没有重复嵌入正文 (M3-2 的完成判据).

用法: uv run python scripts/build_embedding_cache.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import OpenAI

from vendorguard.config import load_settings
from vendorguard.retrieval.chunk_list import load_chunk_list
from vendorguard.retrieval.embedding import load_or_build_vectors

REPO_ROOT = Path(__file__).resolve().parents[1]
CHUNK_LIST_PATH = REPO_ROOT / "data" / "retrieval" / "chunks_v1.jsonl"
ENV_PATH = REPO_ROOT / ".env"


class _CountingClient:
    """在真实客户端外包一层调用计数, 让"有没有重复嵌入"直接可见."""

    def __init__(self, inner: OpenAI) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def embeddings(self) -> _CountingClient:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner.embeddings.create(**kwargs)


def main() -> int:
    """读片段清单与配置, 生成或复用向量缓存, 打印统计与调用次数."""

    env = {**dotenv_values(ENV_PATH), **os.environ}
    api_key = env.get("VENDORGUARD_LLM_API_KEY")
    base_url = env.get("VENDORGUARD_LLM_BASE_URL")
    if not api_key or not base_url:
        print("缺少 VENDORGUARD_LLM_API_KEY 或 VENDORGUARD_LLM_BASE_URL, 先在 .env 里配置")
        return 2

    settings = load_settings()
    records = load_chunk_list(CHUNK_LIST_PATH)
    cache_path = Path(settings.embedding_cache_path)
    client = _CountingClient(
        OpenAI(api_key=str(api_key), base_url=str(base_url), timeout=60.0, max_retries=0)
    )

    vectors = load_or_build_vectors(
        records,
        client=client,  # type: ignore[arg-type]
        model=settings.embedding_model,
        cache_path=cache_path,
    )

    print(f"模型: {vectors.model}")
    print(f"片段: {len(records)} 条, 向量: {len(vectors.vectors)} 条 x {vectors.dimension} 维")
    print(f"接口调用: {client.calls} 次 ({'重建缓存' if client.calls else '复用缓存'})")
    print(f"缓存: {cache_path} ({cache_path.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
