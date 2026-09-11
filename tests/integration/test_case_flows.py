from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vendorguard.admission import (
    AdmissionCase,
    AdmissionCaseStatus,
    evaluate_admission_case,
)
from vendorguard.audit import (
    AuditEvent,
    AuditEventType,
    AuditSubjectType,
    list_audit_events,
)
from vendorguard.config import load_settings
from vendorguard.database import create_database_engine
from vendorguard.policy import (
    StructuredFacts,
    load_demo_case_facts,
    load_policy,
)
from vendorguard.security import User
from vendorguard.suppliers import Supplier

POLICY_PATH = Path("policies/rules/v1.0.0.yaml")
NORMAL_CASE_PATH = Path("data/demo/cases/normal_admission.yaml")


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """提供绑定回滚事务的 Session, 允许多次评估在同一事务内累积审计。"""

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


async def _make_draft_case(
    session: AsyncSession,
) -> tuple[AdmissionCase, User]:
    """在回滚事务内创建候选供应商与 draft 案件, 返回案件与提交专员。"""

    specialist = await session.scalar(select(User).where(User.username == "demo.specialist"))
    assert specialist is not None

    supplier = Supplier(
        display_name="契约流程演示供应商",
        declared_registration_id=f"DEMO-{uuid4()}",
        category_code="standard_components",
    )
    session.add(supplier)
    await session.flush()

    admission_case = AdmissionCase(
        supplier_id=supplier.id,
        submitted_by_user_id=specialist.id,
        status=AdmissionCaseStatus.DRAFT,
    )
    session.add(admission_case)
    await session.flush()
    return admission_case, specialist


def _facts(documents_complete: bool) -> StructuredFacts:
    """构造营业执照正常、材料齐备状态可指定的模拟事实。"""

    return StructuredFacts(
        business_license_document_status="valid",
        category_required_documents_complete=documents_complete,
        sources={
            "business_license_document_status": "doc-license@page:1",
            "category_required_documents_complete": "doc-list@page:1",
        },
    )


async def _evaluate(
    session: AsyncSession,
    admission_case: AdmissionCase,
    facts: StructuredFacts,
    actor_user_id: UUID,
) -> tuple[AdmissionCase, list[AuditEvent]]:
    """用默认策略评估指定案件事实。"""

    result = await evaluate_admission_case(
        session,
        admission_case_id=admission_case.id,
        facts=facts,
        policy=load_policy(POLICY_PATH),
        actor_user_id=actor_user_id,
    )
    assert result is not None
    return result


async def test_normal_admission_yaml_drives_case_to_analyzing(
    session: AsyncSession,
) -> None:
    """正常准入案例事实由 YAML 加载, 评估后停在 analyzing 且不产生任何规则命中。"""

    facts = load_demo_case_facts(NORMAL_CASE_PATH)
    # 事实必须能定位到 fixture 声明的模拟来源。
    assert facts.sources["business_license_document_status"] == "doc-demo-normal-license@page:1"

    admission_case, specialist = await _make_draft_case(session)

    updated_case, events = await _evaluate(session, admission_case, facts, specialist.id)

    assert updated_case.status == AdmissionCaseStatus.ANALYZING
    assert [event.event_type.value for event in events] == [
        "admission_case_status_changed",
        "rules_evaluated",
    ]
    assert not any(event.event_type is AuditEventType.RULE_HIT_RECORDED for event in events)


async def test_supplement_flow_retains_ven002_history_after_rerun(
    session: AsyncSession,
) -> None:
    """补件案首轮命中 VEN-002 进待补件, 重跑回 analyzing 后历史命中仍在审计时间线保留。"""

    admission_case, specialist = await _make_draft_case(session)

    first_case, first_events = await _evaluate(
        session, admission_case, _facts(documents_complete=False), specialist.id
    )
    assert first_case.status == AdmissionCaseStatus.PENDING_DOCUMENTS
    assert any(
        event.event_type is AuditEventType.RULE_HIT_RECORDED
        and event.payload["rule_id"] == "VEN-002"
        for event in first_events
    )

    second_case, second_events = await _evaluate(
        session, admission_case, _facts(documents_complete=True), specialist.id
    )
    assert second_case.status == AdmissionCaseStatus.ANALYZING
    assert not any(event.event_type is AuditEventType.RULE_HIT_RECORDED for event in second_events)

    # append-only 审计保留首轮的 VEN-002 命中, 重跑不覆盖历史。
    timeline = await list_audit_events(
        session,
        subject_type=AuditSubjectType.ADMISSION_CASE,
        subject_id=admission_case.id,
    )
    retained_hits = [
        event
        for event in timeline
        if event.event_type is AuditEventType.RULE_HIT_RECORDED
        and event.payload["rule_id"] == "VEN-002"
    ]
    assert len(retained_hits) == 1
    assert retained_hits[0].payload["action"] == "request_documents"
