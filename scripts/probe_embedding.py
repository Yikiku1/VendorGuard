"""探针: 用一次真实调用复核 embedding 模型的四个事实.

M3-2 的实现与测试都建立在四个前提上: 模型名可用, 向量维度, 是否已做 L2 归一,
响应是否带 index 可回映射. 文档只记录了预期值, 这里用一次真实调用确认或证伪,
结论写进 `tests/unit/test_retrieval_embedding.py` 与 M3 实施记录. 换模型或怀疑
服务端行为变化时, 先重跑本脚本, 不要照文档假设.

输入只用仓库里已经公开的制度标题, 不夹带任何私有材料; 脚本不写任何文件.

用法: uv run python scripts/probe_embedding.py
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"

# 方案 §9 记录的预期, 探针的职责就是确认或推翻它们.
EXPECTED_MODEL = "qwen3.7-text-embedding"
EXPECTED_DIMENSION = 1024

# 已归一的话 L2 范数应当贴近 1; 留一点浮点余量再判"未归一".
_NORM_TOLERANCE = 1e-6

PROBE_INPUTS = (
    "供应商准入管理办法",
    "关键物料必需材料",
)


def _report(model: str, inputs: tuple[str, ...], response: object) -> int:
    """打印维度, 范数, index 回映射与用量, 返回退出码."""

    data = getattr(response, "data", None)
    if not isinstance(data, list) or len(data) != len(inputs):
        print(f"返回条数不符: 期望 {len(inputs)}, 实际 {data if data is None else len(data)}")
        return 1

    indexes: list[int] = []
    dimensions: set[int] = set()
    normalized = True
    finite = True
    for item in data:
        vector = [float(value) for value in item.embedding]
        indexes.append(item.index)
        dimensions.add(len(vector))
        norm = math.sqrt(sum(value * value for value in vector))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            normalized = False
        if not all(math.isfinite(value) for value in vector):
            finite = False
        print(f"  index={item.index} 维度={len(vector)} L2范数={norm:.6f}")

    print(f"模型名: {model}")
    print(f"返回条数: {len(data)}")
    print(f"维度: {sorted(dimensions)} (预期 {EXPECTED_DIMENSION})")
    print(f"index 顺序: {indexes} (按输入顺序应为 {list(range(len(inputs)))})")
    print(f"已 L2 归一: {normalized}")
    print(f"全部有限值: {finite}")

    usage = getattr(response, "usage", None)
    if usage is not None:
        print(f"用量: prompt_tokens={getattr(usage, 'prompt_tokens', None)}")

    ok = (
        dimensions == {EXPECTED_DIMENSION}
        and indexes == list(range(len(inputs)))
        and normalized
        and finite
    )
    print(f"结论: {'与文档预期一致' if ok else '与文档预期不一致, 按实测更新测试与记录'}")
    return 0 if ok else 1


def main() -> int:
    """读本地凭证, 发一次 embedding 请求并报告实测结果."""

    env = {**dotenv_values(ENV_PATH), **os.environ}
    api_key = env.get("VENDORGUARD_LLM_API_KEY")
    base_url = env.get("VENDORGUARD_LLM_BASE_URL")
    model = str(env.get("VENDORGUARD_EMBEDDING_MODEL") or EXPECTED_MODEL)
    if not api_key or not base_url:
        print("缺少 VENDORGUARD_LLM_API_KEY 或 VENDORGUARD_LLM_BASE_URL, 先在 .env 里配置")
        return 2

    client = OpenAI(api_key=str(api_key), base_url=str(base_url), timeout=30.0, max_retries=0)
    try:
        response = client.embeddings.create(model=model, input=list(PROBE_INPUTS))
    except Exception as exc:
        # 探针要把任何失败原因原样打出来, 包括鉴权, 配额与模型名错误.
        print(f"调用失败: {type(exc).__name__}: {exc}")
        return 1

    return _report(model, PROBE_INPUTS, response)


if __name__ == "__main__":
    raise SystemExit(main())
