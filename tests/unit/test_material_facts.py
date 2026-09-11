"""提取层事实派生与字面核对的单元测试."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vendorguard.material_facts import (
    ExtractedFacts,
    ExtractionError,
    check_extracted_facts,
    derive_business_license_status,
    verify_literal_sources,
)
from vendorguard.materials import MaterialText, SourceTexts
from vendorguard.policy import load_policy

REFERENCE_DATE = date(2026, 9, 1)

PAGE_ONE = "企业名称: 演示供应商有限公司\n声明有效期至: 2027-08-31\n材料清单已齐备"
PAGE_TWO = "第二页没有日期信息"


@pytest.fixture
def policy():
    """加载项目现行规则配置."""

    return load_policy(Path("policies/rules/v1.0.0.yaml"))


@pytest.fixture
def sources() -> SourceTexts:
    """一份两页材料构成的来源集合."""

    return SourceTexts.from_materials(
        [
            MaterialText(
                material_id="BL-001",
                source_name="bl.pdf",
                sha256="b" * 64,
                page_count=2,
                pages=(PAGE_ONE, PAGE_TWO),
            )
        ]
    )


def test_derive_status_returns_valid_for_future_date() -> None:
    """有效期晚于参考日期派生为 valid."""

    assert (
        derive_business_license_status(
            declared_valid_until=date(2027, 8, 31), reference_date=REFERENCE_DATE
        )
        == "valid"
    )


def test_derive_status_treats_boundary_day_as_valid() -> None:
    """到期日当天仍有效, 与规则配置的 date_boundary 约定一致."""

    assert (
        derive_business_license_status(
            declared_valid_until=REFERENCE_DATE, reference_date=REFERENCE_DATE
        )
        == "valid"
    )


def test_derive_status_returns_expired_after_boundary() -> None:
    """到期日的次日起算过期."""

    assert (
        derive_business_license_status(
            declared_valid_until=date(2026, 8, 31), reference_date=REFERENCE_DATE
        )
        == "expired"
    )


def test_derive_status_returns_none_without_date() -> None:
    """缺日期返回 None, 让缺字段走追问通道而不是被算成某种状态."""

    assert (
        derive_business_license_status(declared_valid_until=None, reference_date=REFERENCE_DATE)
        is None
    )


def test_verify_literal_sources_accepts_matching_value(sources: SourceTexts) -> None:
    """事实值在其声明的来源里字面命中时核对通过."""

    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2027, 8, 31),
        sources={"business_license_declared_valid_until": "BL-001@page:1"},
    )

    assert verify_literal_sources(facts, sources=sources) == [
        "business_license_declared_valid_until"
    ]


def test_verify_literal_sources_rejects_fabricated_date(sources: SourceTexts) -> None:
    """模型编造的日期不在来源文本中, 核对必须失败并拒绝采信."""

    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2030, 1, 1),
        sources={"business_license_declared_valid_until": "BL-001@page:1"},
    )

    with pytest.raises(ExtractionError) as caught:
        verify_literal_sources(facts, sources=sources)

    assert "未出现在所声明的来源" in str(caught.value)


def test_verify_literal_sources_rejects_locator_in_wrong_page(sources: SourceTexts) -> None:
    """日期在另一页却声称来自本页时, 核对必须失败."""

    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2027, 8, 31),
        sources={"business_license_declared_valid_until": "BL-001@page:2"},
    )

    with pytest.raises(ExtractionError):
        verify_literal_sources(facts, sources=sources)


def test_verify_literal_sources_requires_locator(sources: SourceTexts) -> None:
    """给出事实却不给来源时直接拒绝, 不允许无出处的读数."""

    facts = ExtractedFacts(business_license_declared_valid_until=date(2027, 8, 31))

    with pytest.raises(ExtractionError) as caught:
        verify_literal_sources(facts, sources=sources)

    assert "缺少来源定位" in str(caught.value)


def test_verify_literal_sources_checks_boolean_page_exists(sources: SourceTexts) -> None:
    """布尔判断只核对所引用的页码存在, 不宣称判断本身正确."""

    facts = ExtractedFacts(
        category_required_documents_complete=True,
        sources={"category_required_documents_complete": "BL-001@page:9"},
    )

    with pytest.raises(ExtractionError) as caught:
        verify_literal_sources(facts, sources=sources)

    assert "不存在或为空白页" in str(caught.value)


def test_extracted_facts_reject_unknown_source_key() -> None:
    """来源只能引用本次提交的字段, 越界键被 Schema 拒绝."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExtractedFacts(sources={"business_license_document_status": "BL-001@page:1"})


def test_check_extracted_facts_passes_when_no_rule_hits(policy, sources: SourceTexts) -> None:
    """事实可核对且规则均不命中时给出 not_hit, 并派生状态."""

    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2027, 8, 31),
        category_required_documents_complete=True,
        sources={
            "business_license_declared_valid_until": "BL-001@page:1",
            "category_required_documents_complete": "BL-001@page:1",
        },
    )

    result = check_extracted_facts(
        facts, policy=policy, sources=sources, reference_date=REFERENCE_DATE
    )

    assert result.derived_business_license_status == "valid"
    assert [item.result for item in result.evaluations] == ["not_hit", "not_hit"]
    assert result.missing_fields == []


def test_check_extracted_facts_hits_rule_for_expired_material(policy, sources: SourceTexts) -> None:
    """过期材料派生出 expired 并命中 VEN-001 的补件处置."""

    expired_sources = SourceTexts.from_materials(
        [
            MaterialText(
                material_id="BL-002",
                source_name="bl2.pdf",
                sha256="c" * 64,
                page_count=1,
                pages=("声明有效期至: 2026-03-31",),
            )
        ]
    )
    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2026, 3, 31),
        sources={"business_license_declared_valid_until": "BL-002@page:1"},
    )

    result = check_extracted_facts(
        facts, policy=policy, sources=expired_sources, reference_date=REFERENCE_DATE
    )

    assert result.derived_business_license_status == "expired"
    first = result.evaluations[0]
    assert first.rule_id == "VEN-001"
    assert first.result == "hit"
    assert first.outcome is not None
    assert first.outcome.action == "request_documents"


def test_check_extracted_facts_reports_missing_fields_without_guessing(policy, sources) -> None:
    """缺声明有效期时进入 missing_fields, 不被当成任何一种状态."""

    facts = ExtractedFacts(
        category_required_documents_complete=True,
        sources={"category_required_documents_complete": "BL-001@page:1"},
    )

    result = check_extracted_facts(
        facts, policy=policy, sources=sources, reference_date=REFERENCE_DATE
    )

    assert result.missing_fields == ["business_license_document_status"]
    assert result.derived_business_license_status is None


def test_check_extracted_facts_does_not_run_rules_when_verification_fails(
    policy, sources: SourceTexts
) -> None:
    """核对失败时任何规则都不执行, 也不会返回部分结果."""

    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2030, 1, 1),
        sources={"business_license_declared_valid_until": "BL-001@page:1"},
    )

    with pytest.raises(ExtractionError):
        check_extracted_facts(facts, policy=policy, sources=sources, reference_date=REFERENCE_DATE)


def test_check_extracted_facts_accepts_supplement_source(policy) -> None:
    """用户补充可作为事实来源, 且只能按程序登记的轮次引用."""

    sources = SourceTexts.from_materials([])
    sources.add_supplement(1, "供应商补充: 营业执照声明有效期至 2028-12-31")

    facts = ExtractedFacts(
        business_license_declared_valid_until=date(2028, 12, 31),
        sources={"business_license_declared_valid_until": "用户补充@R1"},
    )

    result = check_extracted_facts(
        facts, policy=policy, sources=sources, reference_date=REFERENCE_DATE
    )

    assert result.derived_business_license_status == "valid"
    assert result.input_sources == {"business_license_document_status": "用户补充@R1"}
