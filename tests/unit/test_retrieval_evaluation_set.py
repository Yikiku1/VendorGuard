"""一期固定检索评测集的金标准与冻结语料绑定测试。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from vendorguard.evidence.evaluation import (
    EvaluationSetError,
    load_retrieval_evaluation_set,
    validate_citation_targets,
)
from vendorguard.evidence.ingestion import load_frozen_corpus_plan

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = REPOSITORY_ROOT / "data" / "knowledge"
EVALUATION_SET_PATH = REPOSITORY_ROOT / "data" / "evals" / "rag_phase1.json"


def _load_plan():
    """加载与评测集必须绑定的正式冻结语料。"""

    return load_frozen_corpus_plan(
        corpus_manifest_path=KNOWLEDGE_ROOT / "manifests" / "corpus_v1.json",
        normalized_manifest_path=KNOWLEDGE_ROOT / "normalized" / "manifest.json",
    )


def test_phase_one_evaluation_set_has_fixed_question_distribution() -> None:
    """一期固定 15 题, 不把拒答和对抗题混入正向召回统计。"""

    evaluation_set = load_retrieval_evaluation_set(EVALUATION_SET_PATH)

    assert evaluation_set.corpus_key == "vendorguard_rag_phase_1"
    assert len(evaluation_set.cases) == 15
    assert Counter(case.kind for case in evaluation_set.cases) == {
        "answerable": 6,
        "no_answer": 4,
        "adversarial_negative": 5,
    }


def test_evaluation_targets_resolve_to_frozen_versioned_nodes() -> None:
    """每个可接受或禁止引用都必须对应当前冻结正文中的真实定位符。"""

    evaluation_set = load_retrieval_evaluation_set(EVALUATION_SET_PATH)

    validate_citation_targets(evaluation_set, _load_plan().nodes)


def test_evaluation_set_rejects_a_gold_target_that_does_not_exist(tmp_path: Path) -> None:
    """手工改错定位符必须在运行检索前失败, 不允许产生伪指标。"""

    payload = json.loads(EVALUATION_SET_PATH.read_text(encoding="utf-8"))
    payload["cases"][0]["required_slots"][0]["accepted_citations"][0]["locator"][-1] = "不存在"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    evaluation_set = load_retrieval_evaluation_set(path)
    with pytest.raises(EvaluationSetError, match="不存在的金标准节点"):
        validate_citation_targets(evaluation_set, _load_plan().nodes)
