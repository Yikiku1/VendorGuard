"""检索排序前的时间、角色和条件硬过滤测试。"""

from __future__ import annotations

from datetime import date

from vendorguard.evidence.persistence import KnowledgeRole
from vendorguard.evidence.retrieval import (
    RetrievalCandidate,
    RetrievalQuery,
    filter_eligible_candidates,
)


def _candidate(
    chunk_key: str,
    *,
    role: KnowledgeRole,
    effective_from: date = date(2026, 1, 1),
    effective_until: date | None = None,
    required_conditions: frozenset[str] = frozenset(),
) -> RetrievalCandidate:
    """构造带完整版本资格元数据的测试候选。"""

    return RetrievalCandidate(
        chunk_key=chunk_key,
        edition_key=f"edition_{chunk_key}",
        node_key=f"node_{chunk_key}",
        role=role,
        effective_from=effective_from,
        effective_until=effective_until,
        required_conditions=required_conditions,
    )


def test_direct_policy_query_rejects_background_and_expired_versions_before_ranking() -> None:
    """直接制度检索只能保留 as_of 当天有效的内部直接依据。"""

    current = _candidate("current", role=KnowledgeRole.DIRECT_POLICY)
    expired = _candidate(
        "expired",
        role=KnowledgeRole.DIRECT_POLICY,
        effective_from=date(2023, 1, 1),
        effective_until=date(2025, 12, 31),
    )
    background = _candidate("background", role=KnowledgeRole.BACKGROUND)
    query = RetrievalQuery(
        text="供应商准入营业执照要求",
        as_of=date(2026, 9, 1),
        intent="direct_policy",
    )

    eligible = filter_eligible_candidates(query, (current, expired, background))

    assert eligible == (current,)


def test_conditional_reference_requires_established_case_fact() -> None:
    """条件性法规不能因文本相似而在缺少适用事实时成为候选。"""

    ccc_reference = _candidate(
        "ccc",
        role=KnowledgeRole.CONDITIONAL_REFERENCE,
        required_conditions=frozenset({"product_in_ccc_directory"}),
    )
    query_without_fact = RetrievalQuery(
        text="普通部件是否需要 CCC 证书",
        as_of=date(2026, 9, 1),
        intent="conditional_reference",
    )
    query_with_fact = query_without_fact.model_copy(
        update={"established_conditions": frozenset({"product_in_ccc_directory"})}
    )

    assert filter_eligible_candidates(query_without_fact, (ccc_reference,)) == ()
    assert filter_eligible_candidates(query_with_fact, (ccc_reference,)) == (ccc_reference,)


def test_version_replay_keeps_historical_version_negative_at_its_effective_date() -> None:
    """版本回放不是当前直接制度检索, 可保留历史生效的 version_negative。"""

    historical = _candidate(
        "historical",
        role=KnowledgeRole.VERSION_NEGATIVE,
        effective_from=date(2023, 1, 1),
        effective_until=date(2025, 12, 31),
    )
    query = RetrievalQuery(
        text="历史供应商准入审批权限",
        as_of=date(2025, 12, 31),
        intent="version_replay",
    )

    assert filter_eligible_candidates(query, (historical,)) == (historical,)
