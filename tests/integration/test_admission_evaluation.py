from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission import (
    AdmissionCase,
    AdmissionCaseStatus,
    evaluate_admission_case,
)
from vendorguard.audit import AuditEvent
from vendorguard.config import load_settings
from vendorguard.database import create_database_engine
from vendorguard.policy import StructuredFacts, load_policy
from vendorguard.security import User
from vendorguard.suppliers import Supplier

POLICY_PATH = Path("policies/rules/v1.0.0.yaml")


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """提供绑定回滚事务的 Session, 用例内所有写入在结束时整体回滚。"""

    engine = create_database_engine(load_settings())
    connection = await engine.connect()
    transaction = await connection.begin()
    bound_session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    try:
        yield bound_session
    finally:
        await bound_session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


async def _make_case(
    session: AsyncSession,
    *,
    status: AdmissionCaseStatus,
) -> tuple[AdmissionCase, User]:
    """在回滚事务内创建候选供应商与指定状态的案件, 返回案件与提交专员。"""

    specialist = await session.scalar(select(User).where(User.username == "demo.specialist"))
    assert specialist is not None

    supplier = Supplier(
        display_name="评估编排测试供应商",
        declared_registration_id=f"EVAL-{uuid4()}",
        category_code="standard_components",
    )
    session.add(supplier)
    await session.flush()

    admission_case = AdmissionCase(
        supplier_id=supplier.id,
        submitted_by_user_id=specialist.id,
        status=status,
    )
    session.add(admission_case)
    await session.flush()
    return admission_case, specialist


def _facts(
    *,
    license_status: str | None = "valid",
    documents_complete: bool | None = True,
) -> StructuredFacts:
    """按给定取值组装结构化事实, 只为非 None 字段配来源定位。"""

    sources: dict[str, str] = {}
    kwargs: dict[str, object] = {}
    if license_status is not None:
        kwargs["business_license_document_status"] = license_status
        sources["business_license_document_status"] = "doc-license@page:1"
    if documents_complete is not None:
        kwargs["category_required_documents_complete"] = documents_complete
        sources["category_required_documents_complete"] = "doc-list@page:1"
    kwargs["sources"] = sources
    return StructuredFacts(**kwargs)


def _event_types(events: list[AuditEvent]) -> list[str]:
    """按顺序提取审计事件类型值, 便于断言事件流。"""

    return [event.event_type.value for event in events]


async def test_evaluation_stops_at_analyzing_when_no_rule_hits(
    session: AsyncSession,
) -> None:
    """营业执照正常且材料齐全时两条启用规则均不命中, 案件停在 analyzing 且不落补件。"""

    admission_case, specialist = await _make_case(session, status=AdmissionCaseStatus.DRAFT)

    result = await evaluate_admission_case(
        session,
        admission_case_id=admission_case.id,
        facts=_facts(),
        policy=load_policy(POLICY_PATH),
        actor_user_id=specialist.id,
    )

    assert result is not None
    updated_case, events = result
    assert updated_case.status == AdmissionCaseStatus.ANALYZING
    assert _event_types(events) == [
        "admission_case_status_changed",
        "rules_evaluated",
    ]
    assert events[0].payload == {"from_status": "draft", "to_status": "analyzing"}


async def test_evaluation_requests_documents_for_incomplete_category(
    session: AsyncSession,
) -> None:
    """品类必填材料不齐命中 VEN-002 request_documents, 案件经 analyzing 落到 pending_documents。"""

    admission_case, specialist = await _make_case(session, status=AdmissionCaseStatus.DRAFT)

    result = await evaluate_admission_case(
        session,
        admission_case_id=admission_case.id,
        facts=_facts(documents_complete=False),
        policy=load_policy(POLICY_PATH),
        actor_user_id=specialist.id,
    )

    assert result is not None
    updated_case, events = result
    assert updated_case.status == AdmissionCaseStatus.PENDING_DOCUMENTS
    assert _event_types(events) == [
        "admission_case_status_changed",
        "rules_evaluated",
        "rule_hit_recorded",
        "admission_case_status_changed",
    ]
    hit_event = events[2]
    assert hit_event.payload["rule_id"] == "VEN-002"
    assert hit_event.payload["action"] == "request_documents"
    assert events[3].payload == {
        "from_status": "analyzing",
        "to_status": "pending_documents",
    }


async def test_evaluation_rules_evaluated_only_covers_enabled_rules(
    session: AsyncSession,
) -> None:
    """rules_evaluated 汇总只含启用清单 VEN-001/VEN-002, 延期规则不参与评估。"""

    admission_case, specialist = await _make_case(session, status=AdmissionCaseStatus.DRAFT)

    result = await evaluate_admission_case(
        session,
        admission_case_id=admission_case.id,
        facts=_facts(),
        policy=load_policy(POLICY_PATH),
        actor_user_id=specialist.id,
    )

    assert result is not None
    _, events = result
    evaluated_event = next(e for e in events if e.event_type.value == "rules_evaluated")
    evaluated_rule_ids = cast("list[str]", evaluated_event.payload["evaluated_rule_ids"])
    assert sorted(evaluated_rule_ids) == ["VEN-001", "VEN-002"]


async def test_evaluation_stops_at_analyzing_for_manual_review_outcome(
    session: AsyncSession,
) -> None:
    """缺材料齐全事实时 VEN-002 走 create_manual_review, 非补件动作故案件停在 analyzing。"""

    admission_case, specialist = await _make_case(session, status=AdmissionCaseStatus.DRAFT)

    result = await evaluate_admission_case(
        session,
        admission_case_id=admission_case.id,
        facts=_facts(documents_complete=None),
        policy=load_policy(POLICY_PATH),
        actor_user_id=specialist.id,
    )

    assert result is not None
    updated_case, events = result
    assert updated_case.status == AdmissionCaseStatus.ANALYZING
    assert _event_types(events) == [
        "admission_case_status_changed",
        "rules_evaluated",
        "rule_hit_recorded",
    ]
    assert events[2].payload["action"] == "create_manual_review"


async def test_evaluation_reruns_from_pending_documents_back_to_analyzing(
    session: AsyncSession,
) -> None:
    """补件后从 pending_documents 重跑, 事实齐全则回到 analyzing, 保留可重跑语义。"""

    admission_case, specialist = await _make_case(
        session, status=AdmissionCaseStatus.PENDING_DOCUMENTS
    )

    result = await evaluate_admission_case(
        session,
        admission_case_id=admission_case.id,
        facts=_facts(),
        policy=load_policy(POLICY_PATH),
        actor_user_id=specialist.id,
    )

    assert result is not None
    updated_case, events = result
    assert updated_case.status == AdmissionCaseStatus.ANALYZING
    assert events[0].payload == {
        "from_status": "pending_documents",
        "to_status": "analyzing",
    }


async def test_evaluation_rejects_case_outside_evaluable_state(
    session: AsyncSession,
) -> None:
    """非 draft/pending_documents 的案件不能被评估, 抛 ValueError 且不落状态变化。"""

    admission_case, specialist = await _make_case(
        session, status=AdmissionCaseStatus.PENDING_APPROVAL
    )

    with pytest.raises(ValueError, match="不支持从"):
        await evaluate_admission_case(
            session,
            admission_case_id=admission_case.id,
            facts=_facts(),
            policy=load_policy(POLICY_PATH),
            actor_user_id=specialist.id,
        )


async def test_evaluation_returns_none_for_missing_case(
    session: AsyncSession,
) -> None:
    """评估不存在的案件返回 None, 交由 HTTP 层转统一 404。"""

    specialist = await session.scalar(select(User).where(User.username == "demo.specialist"))
    assert specialist is not None

    result = await evaluate_admission_case(
        session,
        admission_case_id=uuid4(),
        facts=_facts(),
        policy=load_policy(POLICY_PATH),
        actor_user_id=specialist.id,
    )

    assert result is None
