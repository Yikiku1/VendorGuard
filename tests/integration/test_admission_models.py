from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from vendorguard.admission import (
    AdmissionCase,
    AdmissionCaseStatus,
    Document,
    DocumentType,
)
from vendorguard.config import load_settings
from vendorguard.database import create_database_engine, create_session_factory
from vendorguard.security import User
from vendorguard.suppliers import Supplier, SupplierEligibility


async def test_supplier_and_draft_admission_case_are_persisted() -> None:
    settings = load_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            specialist = await session.scalar(
                select(User).where(User.username == "demo.specialist")
            )
            assert specialist is not None

            supplier = Supplier(
                display_name="测试供应商",
                declared_registration_id=f"TEST-{uuid4()}",
                category_code="standard_components",
            )
            session.add(supplier)
            await session.flush()

            admission_case = AdmissionCase(
                supplier_id=supplier.id,
                submitted_by_user_id=specialist.id,
            )
            session.add(admission_case)
            await session.flush()

            supplier_id = supplier.id
            case_id = admission_case.id
            session.expunge_all()

            stored_supplier = await session.get(Supplier, supplier_id)
            stored_case = await session.get(AdmissionCase, case_id)

            assert stored_supplier is not None
            assert stored_case is not None
            assert stored_supplier.eligibility_status == SupplierEligibility.CANDIDATE
            assert stored_case.status == AdmissionCaseStatus.DRAFT
            assert stored_case.supplier_id == stored_supplier.id
            assert stored_case.submitted_by_user_id == specialist.id

            document_sha256 = "a" * 64
            document = Document(
                admission_case_id=stored_case.id,
                uploaded_by_user_id=stored_case.submitted_by_user_id,
                document_type=DocumentType.BUSINESS_LICENSE,
                original_file_name="business_license.pdf",
                media_type="application/pdf",
                file_size_bytes=1024,
                sha256=document_sha256,
            )
            session.add(document)
            await session.flush()

            document_id = document.id
            session.expunge(document)

            stored_document = await session.get(Document, document_id)

            assert stored_document is not None
            assert stored_document.admission_case_id == stored_case.id
            assert stored_document.document_type == DocumentType.BUSINESS_LICENSE
            assert stored_document.file_size_bytes == 1024
            assert stored_document.sha256 == document_sha256

            duplicate_document = Document(
                admission_case_id=stored_case.id,
                uploaded_by_user_id=stored_case.submitted_by_user_id,
                document_type=DocumentType.BUSINESS_LICENSE,
                original_file_name="renamed-license.pdf",
                media_type="application/pdf",
                file_size_bytes=1024,
                sha256=document_sha256,
            )
            session.add(duplicate_document)

            with pytest.raises(
                IntegrityError,
                match="uq_documents_case_sha256",
            ):
                await session.flush()

            await session.rollback()
    finally:
        await engine.dispose()
