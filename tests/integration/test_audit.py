from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from vendorguard.audit import (
    AuditEventType,
    AuditSubjectType,
    append_audit_event,
    list_audit_events,
)
from vendorguard.config import load_settings
from vendorguard.database import create_database_engine, create_session_factory
from vendorguard.security import User


async def test_audit_events_are_appended_and_listed_in_order() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            specialist = await session.scalar(
                select(User).where(User.username == "demo.specialist")
            )
            assert specialist is not None

            subject_id = uuid4()

            first_event = await append_audit_event(
                session,
                subject_type=AuditSubjectType.ADMISSION_CASE,
                subject_id=subject_id,
                event_type=AuditEventType.ADMISSION_CASE_CREATED,
                actor_user_id=specialist.id,
                payload={"supplier_reference": "supplier-demo-001"},
            )
            second_event = await append_audit_event(
                session,
                subject_type=AuditSubjectType.ADMISSION_CASE,
                subject_id=subject_id,
                event_type=AuditEventType.DOCUMENTS_REGISTERED,
                actor_user_id=specialist.id,
                payload={"document_count": 1},
            )

            events = await list_audit_events(
                session,
                subject_type=AuditSubjectType.ADMISSION_CASE,
                subject_id=subject_id,
            )

            assert [event.id for event in events] == [
                first_event.id,
                second_event.id,
            ]
            assert [event.event_type.value for event in events] == [
                "admission_case_created",
                "documents_registered",
            ]
            assert events[0].actor_user_id == specialist.id
            assert events[0].payload == {"supplier_reference": "supplier-demo-001"}
            assert events[1].payload == {"document_count": 1}

            await session.rollback()
    finally:
        await engine.dispose()


async def test_audit_event_cannot_be_updated() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            event = await append_audit_event(
                session,
                subject_type=AuditSubjectType.ADMISSION_CASE,
                subject_id=uuid4(),
                event_type=AuditEventType.ADMISSION_CASE_CREATED,
                actor_user_id=None,
                payload={"supplier_reference": "original"},
            )

            event.payload = {"supplier_reference": "tampered"}

            with pytest.raises(
                DBAPIError,
                match="audit events are append-only",
            ):
                await session.flush()

            await session.rollback()
    finally:
        await engine.dispose()


async def test_audit_event_cannot_be_deleted() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            event = await append_audit_event(
                session,
                subject_type=AuditSubjectType.ADMISSION_CASE,
                subject_id=uuid4(),
                event_type=AuditEventType.ADMISSION_CASE_CREATED,
                actor_user_id=None,
                payload={"supplier_reference": "must-be-retained"},
            )

            await session.delete(event)

            with pytest.raises(
                DBAPIError,
                match="audit events are append-only",
            ):
                await session.flush()

            await session.rollback()
    finally:
        await engine.dispose()
