from pathlib import Path

from vendorguard.policy import evaluate_rule, load_policy

POLICY_PATH = Path("policies/rules/v1.0.0.yaml")


def test_ven_001_does_not_hit_for_valid_business_license() -> None:
    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-001")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={"business_license_document_status": "valid"},
    )

    assert evaluation.rule_id == "VEN-001"
    assert evaluation.result == "not_hit"
    assert evaluation.outcome is None


def test_ven_001_requests_documents_for_expired_business_license() -> None:
    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-001")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={"business_license_document_status": "expired"},
    )

    assert evaluation.rule_id == "VEN-001"
    assert evaluation.result == "hit"

    outcome = evaluation.outcome
    assert outcome is not None
    assert outcome.action == "request_documents"
    assert outcome.blocking is True
    assert outcome.target_case_status == "pending_documents"
    assert outcome.reason_code == "business_license_expired"


def test_ven_001_requests_documents_when_business_license_is_missing() -> None:
    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-001")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={},
    )

    assert evaluation.rule_id == "VEN-001"
    assert evaluation.result == "hit"

    outcome = evaluation.outcome
    assert outcome is not None
    assert outcome.action == "request_documents"
    assert outcome.reason_code == "business_license_missing"
