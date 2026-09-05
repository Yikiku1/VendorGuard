from datetime import date
from pathlib import Path

import pytest

from vendorguard.policy import (
    FactsValidationError,
    StructuredFacts,
    certificate_remaining_days,
    load_demo_case_facts,
    load_policy,
)

POLICY_PATH = Path("policies/rules/v1.0.0.yaml")
NORMAL_CASE_PATH = Path("data/demo/cases/normal_admission.yaml")
SUPPLEMENT_CASE_PATH = Path("data/demo/cases/supplement_review.yaml")


def test_normal_admission_yaml_loads_complete_facts() -> None:
    """正常准入案例 YAML 能加载出完整事实, 且来源定位可复算到模拟页码。"""

    facts = load_demo_case_facts(NORMAL_CASE_PATH)

    assert facts.business_license_document_status == "valid"
    assert facts.category_required_documents_complete is True
    assert facts.sources["business_license_document_status"] == "doc-demo-normal-license@page:1"

    # 事实字段白名单必须覆盖启用规则的全部 input_fields, 否则评估会静默缺字段。
    policy = load_policy(POLICY_PATH)
    enabled_ids = set(policy.implementation_scope.enabled_rule_ids)
    enabled_input_fields = {
        field for rule in policy.rules if rule.id in enabled_ids for field in rule.input_fields
    }
    assert enabled_input_fields <= set(StructuredFacts.model_fields)
    # 白名单外字段必须被拒, 直接读类属性 model_config (而非 type(类)) 以避开实例访问弃用告警。
    assert StructuredFacts.model_config["extra"] == "forbid"


def test_supplement_after_stage_yaml_loads_facts() -> None:
    """补件案例 after_supplement 段子段能加载出该阶段事实与来源定位。"""

    facts = load_demo_case_facts(SUPPLEMENT_CASE_PATH, stage="after_supplement")

    assert facts.business_license_document_status == "valid"
    # 案例 YAML 的 after_supplement 段未声明材料齐全事实, 加载器不猜测缺失字段。
    assert facts.category_required_documents_complete is None


def test_facts_without_source_location_are_rejected() -> None:
    """已给出的事实必须带来源定位, 缺来源直接报错而不是静默通过。"""

    with pytest.raises(FactsValidationError, match="缺少来源定位"):
        StructuredFacts(
            business_license_document_status="valid",
            category_required_documents_complete=True,
            sources={},
        )


def test_unknown_fact_field_is_rejected() -> None:
    """白名单之外的未知事实字段必须拒绝, 防止拼写错误静默丢失。"""

    with pytest.raises(FactsValidationError, match="extra_forbidden"):
        StructuredFacts(
            **{
                "business_license_document_status": "valid",
                "category_required_documents_complete": True,
                "sources": {
                    "business_license_document_status": "doc-license@page:1",
                    "category_required_documents_complete": "doc-list@page:1",
                },
                "quote_deviation_percent": 8.0,
                "quote_deviation_percent_source": "doc-quote@A2",
            }
        )


def test_boolean_fact_rejects_string_false() -> None:
    """字符串 "false" 不是布尔事实, 序列化事故必须在事实层被拦截。"""

    with pytest.raises(FactsValidationError, match="bool_type"):
        StructuredFacts(
            business_license_document_status="valid",
            category_required_documents_complete="false",
            sources={
                "business_license_document_status": "doc-license@page:1",
            },
        )


def test_license_status_rejects_unknown_value() -> None:
    """营业执照状态只允许固定枚举值, 状态外取值不输出猜测结果。"""

    with pytest.raises(FactsValidationError, match="business_license_document_status"):
        StructuredFacts(
            business_license_document_status="definitely_valid",
            category_required_documents_complete=True,
            sources={
                "business_license_document_status": "doc-license@page:1",
                "category_required_documents_complete": "doc-list@page:1",
            },
        )


def test_source_location_must_contain_document_id() -> None:
    """来源定位必须形如 document_id@定位符, 只有定位符没有材料编号要拒绝。"""

    with pytest.raises(FactsValidationError, match="来源定位格式非法"):
        StructuredFacts(
            business_license_document_status="valid",
            category_required_documents_complete=True,
            sources={
                "business_license_document_status": "@page:1",
                "category_required_documents_complete": "doc-list@page:1",
            },
        )


def test_certificates_without_source_are_not_required() -> None:
    """营业执照事实为 None 表示案件确实没有该材料, 不要求伪造来源定位。"""

    facts = StructuredFacts(
        business_license_document_status=None,
        category_required_documents_complete=True,
        sources={
            "category_required_documents_complete": "doc-list@page:1",
        },
    )

    assert facts.business_license_document_status is None


def test_certificate_remaining_days_counts_expiry_day_as_valid() -> None:
    """到期日当天仍有效: 2026-09-01 到 2026-09-30 剩余 29 天。"""

    assert (
        certificate_remaining_days(
            valid_until=date(2026, 9, 30),
            reference_date=date(2026, 9, 1),
        )
        == 29
    )


def test_certificate_remaining_days_rejects_missing_valid_until() -> None:
    """缺少有效期日期时明确报错, 不猜测一个默认到期日。"""

    with pytest.raises(FactsValidationError, match="valid_until"):
        certificate_remaining_days(
            valid_until=None,
            reference_date=date(2026, 9, 1),
        )
