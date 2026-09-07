"""一期固定检索评测集的契约测试。"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from vendorguard.evidence.evaluation import (
    CitationTarget,
    RetrievalEvaluationLoadError,
    RetrievalEvaluationSet,
    load_retrieval_evaluation_set,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
CORPUS_MANIFEST_PATH = KNOWLEDGE_DIR / "manifests" / "corpus_v1.json"
NORMALIZED_MANIFEST_PATH = KNOWLEDGE_DIR / "normalized" / "manifest.json"
EVALUATION_PATH = REPO_ROOT / "data" / "evals" / "rag_phase1.json"


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象, 使测试中的清单读取保持一致。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _evaluation_set() -> RetrievalEvaluationSet:
    """加载一期冻结评测集。"""

    return load_retrieval_evaluation_set(EVALUATION_PATH)


def _corpus_entries() -> dict[str, dict[str, Any]]:
    """返回正式版本键到清单条目的映射。"""

    entries = _read_json(CORPUS_MANIFEST_PATH)["entries"]
    assert isinstance(entries, list)
    return {entry["edition_key"]: entry for entry in entries}


def _normalized_texts() -> dict[str, str]:
    """返回正式版本键到归一化正文的映射。"""

    entries = _read_json(NORMALIZED_MANIFEST_PATH)["entries"]
    assert isinstance(entries, list)
    return {
        entry["edition_key"]: (KNOWLEDGE_DIR / entry["normalized_path"]).read_text(encoding="utf-8")
        for entry in entries
    }


def _is_effective(entry: dict[str, Any], as_of: date) -> bool:
    """按项目既有闭区间语义判断版本在评测日期是否有效。"""

    effective_from = date.fromisoformat(entry["effective_from"])
    effective_until = entry["effective_until"]

    return effective_from <= as_of and (
        effective_until is None or as_of <= date.fromisoformat(effective_until)
    )


def _assert_locator_resolves(target: CitationTarget, normalized_text: str) -> None:
    """校验定位符能回指正文标题, 表格行号也落在实际数据行内。"""

    first_locator, *remaining_locators = target.locator
    assert first_locator in normalized_text

    if not remaining_locators:
        return

    assert len(remaining_locators) == 1
    row_match = re.fullmatch(r"第([1-9][0-9]*)行", remaining_locators[0])
    assert row_match is not None

    section_text = normalized_text.split(first_locator, maxsplit=1)[1]
    section_text = re.split(r"\n## ", section_text, maxsplit=1)[0]
    table_lines = [line for line in section_text.splitlines() if line.strip().startswith("|")]

    assert len(table_lines) >= 3
    data_rows = table_lines[2:]
    row_number = int(row_match.group(1))
    assert row_number <= len(data_rows)


def test_phase_one_evaluation_set_has_fixed_question_mix() -> None:
    """一期评测集固定为 15 题, 保持正向、拒答与对抗题的约定配比。"""

    evaluation_set = _evaluation_set()
    kinds = [case.kind for case in evaluation_set.cases]

    assert evaluation_set.schema_version == "1.0"
    assert evaluation_set.dataset_key == "rag_phase1"
    assert len(evaluation_set.cases) == 15
    assert kinds.count("answerable") == 6
    assert kinds.count("no_answer") == 4
    assert kinds.count("adversarial_negative") == 5


def test_gold_targets_exist_are_effective_and_have_resolvable_locators() -> None:
    """有答案题的金标准必须存在, 在题目日期有效且能定位到正式正文。"""

    corpus_entries = _corpus_entries()
    normalized_texts = _normalized_texts()

    for case in _evaluation_set().cases:
        for slot in case.required_slots:
            for target in slot.targets:
                assert target.edition_key in corpus_entries
                assert target.edition_key in normalized_texts
                assert _is_effective(corpus_entries[target.edition_key], case.as_of)
                _assert_locator_resolves(target, normalized_texts[target.edition_key])


def test_forbidden_versions_exist_and_never_overlap_gold_targets() -> None:
    """对抗题和拒答题使用的禁止版本必须可追溯且不可覆盖金标准。"""

    corpus_entries = _corpus_entries()

    for case in _evaluation_set().cases:
        gold_edition_keys = {
            target.edition_key for slot in case.required_slots for target in slot.targets
        }

        assert gold_edition_keys.isdisjoint(case.forbidden_edition_keys)
        assert all(edition_key in corpus_entries for edition_key in case.forbidden_edition_keys)


def test_no_answer_cases_have_no_required_citations() -> None:
    """拒答题不能暗藏金标准引用, 防止将伪引用误计为正确回答。"""

    for case in _evaluation_set().cases:
        if case.expected_outcome == "no_answer":
            assert not case.required_slots
            assert case.forbidden_edition_keys


def test_loader_rejects_non_object_json_payload(tmp_path: Path) -> None:
    """评测集必须是 JSON 对象, 顶层数组不能被静默当作空数据处理。"""

    invalid_path = tmp_path / "invalid_evaluation.json"
    invalid_path.write_text("[]", encoding="utf-8")

    with pytest.raises(RetrievalEvaluationLoadError, match="顶层必须是对象"):
        load_retrieval_evaluation_set(invalid_path)


def test_loader_rejects_duplicate_case_id(tmp_path: Path) -> None:
    """题目标识重复会使离线指标归属不确定, 必须在加载时拒绝。"""

    payload = _read_json(EVALUATION_PATH)
    cases = payload["cases"]
    assert isinstance(cases, list)
    cases[1]["case_id"] = cases[0]["case_id"]

    invalid_path = tmp_path / "duplicate_case_id.json"
    invalid_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(RetrievalEvaluationLoadError, match="未通过 Schema 校验"):
        load_retrieval_evaluation_set(invalid_path)
