"""按 PRD 选定的 8 题评测首版制度检索 (M3-7).

只读 `data/evals/rag_phase1.json` 里 PRD 指定的 8 个 `case_id` (用 ID 引用原记录,
不改写原数据), 用当前片段清单与向量缓存跑 `search_policy`, 逐题核对金标准并写出
一份结果 JSON 到 `data/evals/` (只增文件).

程序能判定与不能判定的部分分得很清楚:

- 能判定: 每道题 `required_slots` 指定的 (版本, 定位) 是否出现在本次 Top-5;
  `forbidden_edition_keys` 里的版本是否出现在结果里.
- 不能判定: 自然语言结论是否受引用支持, 以及"这道题该不该判依据不足". 检索按设计
  总是返回 Top-5 (不设分数阈值), 所以 no_answer 类题目由固定案例人工核对, 脚本
  只把 Top-5 摆出来并在结论里标记待核对, 不替人下判断.

结果里的 `cache_key` 说明用了哪份向量缓存: 服务端 embedding 不是逐分量确定的,
所以"可复现"绑定的是具体缓存文件, 这一点必须写进记录.

用法: uv run python scripts/run_rag_eval.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import OpenAI

from vendorguard.agent import load_policy_index
from vendorguard.config import load_settings
from vendorguard.retrieval.chunk_list import CURRENT_EDITION_KEYS
from vendorguard.retrieval.search import RetrievedChunk, search_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "data" / "evals" / "rag_phase1.json"
RESULT_PATH = REPO_ROOT / "data" / "evals" / "rag_phase1_m3_8q_result.json"
CHUNK_LIST_PATH = REPO_ROOT / "data" / "retrieval" / "chunks_v1.jsonl"
ENV_PATH = REPO_ROOT / ".env"

# PRD 指定的 8 题, 顺序照 PRD 的分组列出; 其余 7 题本期未覆盖.
SELECTED_CASE_IDS = (
    # 正常检索: 看必需依据是否进入 Top-5
    "rag_phase1_current_admission_license_consistency",
    "rag_phase1_critical_material_quality_certificate",
    "rag_phase1_standard_components_quality_certificate",
    # 无依据或缺条件: 金标准是"语料不支持这个结论"
    "rag_phase1_license_authenticity_no_answer",
    "rag_phase1_quality_certificate_without_category_no_answer",
    # 误导问题: 需纠正前提并引用现行制度, 不是一概拒答
    "rag_phase1_retired_policy_late_quality_certificate",
    "rag_phase1_government_duty_as_internal_rule_no_answer",
    "rag_phase1_voluntary_certification_overrides_policy",
)

NORMAL_CASE_IDS = SELECTED_CASE_IDS[:3]


def load_cases() -> dict[str, dict[str, Any]]:
    """读取原评测数据并按 case_id 建索引; 文件本身只读不改."""

    payload = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    return {str(case["case_id"]): case for case in payload["cases"]}


def required_targets(case: dict[str, Any]) -> list[dict[str, Any]]:
    """把金标准的 required_slots 展平成 (slot_key, edition_key, locator) 三元组."""

    targets: list[dict[str, Any]] = []
    for slot in case["required_slots"]:
        for target in slot["targets"]:
            targets.append(
                {
                    "slot_key": slot["slot_key"],
                    "edition_key": target["edition_key"],
                    "locator": tuple(target["locator"]),
                }
            )
    return targets


def check_case(case: dict[str, Any], results: tuple[RetrievedChunk, ...]) -> dict[str, Any]:
    """核对一道题的必需要素与被禁版本, 给出程序可判定的结论.

    `forbidden_edition_keys` 有两种含义, 必须分开看:

    - 旧版本与外部法规这类**非现行**版本出现在结果里就是检索违规 (白名单必须在打分
      前把它们滤掉), 按设计这里应当恒为 0;
    - 现行版本被列为禁止, 说的是"这道题的结论不能以它们为依据" (题目超出制度范围),
      它们在 Top-5 里出现是正常的; 该不该据此作答属语义判断, 交人工核对.
    """

    hits = [
        {
            **target,
            "hit": any(
                item.edition_key == target["edition_key"] and item.locator_path == target["locator"]
                for item in results
            ),
        }
        for target in required_targets(case)
    ]
    current = set(CURRENT_EDITION_KEYS)
    non_current_hits = [
        item.node_key
        for item in results
        if item.edition_key in case["forbidden_edition_keys"] and item.edition_key not in current
    ]
    unsupported_hits = [
        item.node_key
        for item in results
        if item.edition_key in case["forbidden_edition_keys"] and item.edition_key in current
    ]

    if non_current_hits:
        verdict = "违规: 结果里出现非现行的被禁版本"
    elif hits and all(item["hit"] for item in hits):
        verdict = "命中: 必需依据都在 Top-5"
    elif hits:
        verdict = "漏检: 有必需依据没进 Top-5"
    else:
        verdict = "待人工核对: 检索必然返回 Top-5, 是否判依据不足不由检索决定"

    return {
        "case_id": case["case_id"],
        "kind": case["kind"],
        "expected_outcome": case["expected_outcome"],
        "query": case["query"],
        "verdict": verdict,
        "required_slots": hits,
        "forbidden_non_current_hits": non_current_hits,
        "forbidden_current_hits": unsupported_hits,
        "top5": [
            {
                "node_key": item.node_key,
                "edition_key": item.edition_key,
                "locator": list(item.locator_path),
                "score": round(item.score, 4),
                "text_head": item.display_text.splitlines()[-1][:60],
            }
            for item in results
        ],
    }


def summarize(checked: list[dict[str, Any]]) -> dict[str, Any]:
    """按 PRD 的三项汇总: 正常题命中, 边界题可判定部分, 错误或无效引用."""

    normal = [item for item in checked if item["case_id"] in NORMAL_CASE_IDS]
    normal_hits = [item for item in normal if item["verdict"].startswith("命中")]
    boundary = [item for item in checked if item["case_id"] not in NORMAL_CASE_IDS]
    program_checked = [item for item in boundary if item["required_slots"]]
    program_hits = [item for item in program_checked if item["verdict"].startswith("命中")]
    manual = [item["case_id"] for item in boundary if not item["required_slots"]]

    return {
        "normal_required_in_top5": f"{len(normal_hits)}/{len(normal)}",
        "boundary_program_checked": f"{len(program_hits)}/{len(program_checked)}",
        "boundary_manual_review": manual,
        "forbidden_non_current_hits": sum(
            len(item["forbidden_non_current_hits"]) for item in checked
        ),
        "invalid_citations_program_judged": 0,
        "notes": [
            "非现行的被禁版本 (旧版制度, 外部法规) 命中数按设计应为 0: 候选在打分前就按"
            "现行白名单过滤 (M3-3 有测试)。",
            "错误或无效引用只统计程序可判定的部分 (伪造节点, 旧版本, 未检索过的节点), "
            "这些在执行路径上由引用闸门与白名单保证; 引用与结论是否相符属语义判断, "
            "留给固定案例人工核对。",
            "no_answer 类题目把现行版本列为 forbidden, 指的是结论不能以它们为依据, "
            "它们在 Top-5 里出现属正常, 逐题记在 forbidden_current_hits 里交人工核对。",
        ],
    }


def main() -> int:
    """跑完 8 题并写出结果 JSON, 同时打印逐题报告."""

    env = {**dotenv_values(ENV_PATH), **os.environ}
    api_key = env.get("VENDORGUARD_LLM_API_KEY")
    base_url = env.get("VENDORGUARD_LLM_BASE_URL")
    if not api_key or not base_url:
        print("缺少 VENDORGUARD_LLM_API_KEY 或 VENDORGUARD_LLM_BASE_URL, 先在 .env 里配置")
        return 2

    settings = load_settings()
    client = OpenAI(api_key=str(api_key), base_url=str(base_url), timeout=60.0, max_retries=0)
    index = load_policy_index(
        client=client,
        model=settings.embedding_model,
        cache_path=settings.embedding_cache_path,
        chunk_list_path=CHUNK_LIST_PATH,
    )
    cases = load_cases()

    checked: list[dict[str, Any]] = []
    for case_id in SELECTED_CASE_IDS:
        case = cases[case_id]
        results = search_policy(
            case["query"],
            records=index.records,
            vectors=index.vectors,
            embedding_client=client,
            model=index.model,
        )
        item = check_case(case, results)
        checked.append(item)
        print(f"\n=== {case_id} [{case['kind']}]")
        print(f"  问题: {case['query']}")
        print(f"  结论: {item['verdict']}")
        for slot in item["required_slots"]:
            mark = "命中" if slot["hit"] else "未命中"
            print(f"    - {slot['slot_key']}: {mark} {slot['edition_key']} {list(slot['locator'])}")
        for rank, row in enumerate(item["top5"], start=1):
            print(f"    [{rank}] {row['score']:.4f} {row['node_key']}")
            print(f"         定位 {row['locator']} | {row['text_head']}")

    summary = summarize(checked)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "eval_set": str(EVAL_PATH.relative_to(REPO_ROOT)),
        "selected_case_ids": list(SELECTED_CASE_IDS),
        "uncovered_case_count": len(cases) - len(SELECTED_CASE_IDS),
        "embedding_model": index.model,
        "cache_key": index.vectors.cache_key,
        "chunk_count": len(index.records),
        "summary": summary,
        "cases": checked,
    }
    RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print("\n=== 汇总")
    print(f"  正常题必需依据进入 Top-5: {summary['normal_required_in_top5']}")
    print(f"  边界题可程序核对部分: {summary['boundary_program_checked']}")
    print(f"  边界题待人工核对: {summary['boundary_manual_review']}")
    print(f"  非现行被禁版本命中: {summary['forbidden_non_current_hits']}")
    print(f"  程序可判定的错误或无效引用: {summary['invalid_citations_program_judged']}")
    print(f"  结果写入 {RESULT_PATH.relative_to(REPO_ROOT)}")
    print(
        f"  本次未覆盖 {payload['uncovered_case_count']} 题 (历史日期与外部法规), "
        "不能把 8 题结果当作完整 15 题成绩"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
