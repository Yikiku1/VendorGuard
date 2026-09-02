from pathlib import Path

import pytest

from vendorguard.policy.schema import PolicyLoadError, load_policy

POLICY_PATH = Path("policies/rules/v1.0.0.yaml")


def test_load_versioned_policy_schema() -> None:
    policy = load_policy(POLICY_PATH)

    assert policy.schema_version == "1.0"
    assert policy.version == "1.0.0"
    assert policy.implementation_scope.enabled_rule_ids == [
        "VEN-001",
        "VEN-002",
        "VEN-004",
        "VEN-005",
        "PR-001",
    ]
    assert len(policy.rules) == 7


def test_reject_unknown_operator(tmp_path: Path) -> None:
    invalid_policy = (POLICY_PATH.read_text(encoding="utf-8")).replace(
        "operator: in", "operator: equals_any"
    )
    policy_path = tmp_path / "invalid.yaml"
    policy_path.write_text(invalid_policy, encoding="utf-8")

    with pytest.raises(PolicyLoadError):
        load_policy(policy_path)


def test_reject_duplicate_rule_ids(tmp_path: Path) -> None:
    invalid_policy = (POLICY_PATH.read_text(encoding="utf-8")).replace(
        "- id: VEN-002", "- id: VEN-001"
    )
    policy_path = tmp_path / "invalid.yaml"
    policy_path.write_text(invalid_policy, encoding="utf-8")

    with pytest.raises(PolicyLoadError):
        load_policy(policy_path)
