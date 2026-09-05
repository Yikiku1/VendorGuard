from pathlib import Path

from vendorguard.policy import evaluate_rule, load_policy

POLICY_PATH = Path("policies/rules/v1.0.0.yaml")


def test_ven_001_does_not_hit_for_valid_business_license() -> None:
    """营业执照状态正常时 VEN-001 不命中, 也不返回处置结果。"""

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
    """营业执照过期时 VEN-001 命中, 按字段值路由到阻塞补件并进入 pending_documents。"""

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
    """缺 business_license_document_status 事实时走 YAML 的 on_missing_input, 即补件动作。

    与 VEN-002 缺输入走 create_manual_review 形成关键差异。
    """
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


def test_ven_002_requests_documents_when_required_documents_missing() -> None:
    """品类必填材料不齐时应命中, 走补件动作并进入 pending_documents 状态。"""

    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-002")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={"category_required_documents_complete": False},
    )

    assert evaluation.rule_id == "VEN-002"
    assert evaluation.result == "hit"

    outcome = evaluation.outcome
    assert outcome is not None
    assert outcome.action == "request_documents"
    assert outcome.target_case_status == "pending_documents"
    assert outcome.reason_code == "category_required_documents_missing"
    # YAML 未配置 blocking, 实现者不能"顺手"补默认值。
    assert outcome.blocking is None


def test_ven_002_does_not_hit_when_documents_complete() -> None:
    """品类必填材料齐全时不命中, 也不返回处置结果。"""

    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-002")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={"category_required_documents_complete": True},
    )

    assert evaluation.rule_id == "VEN-002"
    assert evaluation.result == "not_hit"
    assert evaluation.outcome is None


def test_ven_002_creates_manual_review_when_fact_missing() -> None:
    """缺 category_required_documents_complete 事实时走人工复核, 而不是补件。

    上游未告知该品类有哪些必填材料时, 让用户补件没有目标, 因此 YAML 明确
    指定 on_missing_input 使用 create_manual_review。这与 VEN-001 缺输入
    走 request_documents 形成关键差异。
    """

    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-002")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={},
    )

    assert evaluation.result == "hit"
    outcome = evaluation.outcome
    assert outcome is not None
    assert outcome.action == "create_manual_review"
    assert outcome.reason_code == "document_completeness_unknown"
    assert outcome.target_case_status is None


def test_ven_002_not_applicable_for_procurement_exception_scope() -> None:
    """VEN-002 只声明 supplier_admission 作用域, 其他作用域返回 not_applicable。"""

    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-002")

    evaluation = evaluate_rule(
        rule,
        scope="procurement_exception",
        facts={"category_required_documents_complete": False},
    )

    assert evaluation.result == "not_applicable"
    assert evaluation.outcome is None


def test_ven_002_does_not_hit_for_string_false() -> None:
    """字符串 "false" 不能误命中布尔规则, 防止常见序列化事故。"""

    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-002")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={"category_required_documents_complete": "false"},
    )

    assert evaluation.result == "not_hit"


def test_ven_002_does_not_hit_for_zero() -> None:
    """数字 0 不能命中 equals: false 的规则, 防 Python `0 == False` 跨类型陷阱。"""

    policy = load_policy(POLICY_PATH)
    rule = next(rule for rule in policy.rules if rule.id == "VEN-002")

    evaluation = evaluate_rule(
        rule,
        scope="supplier_admission",
        facts={"category_required_documents_complete": 0},
    )

    assert evaluation.result == "not_hit"
