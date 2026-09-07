"""知识版本快照模型的契约测试。"""

from datetime import date

import pytest
from pydantic import ValidationError

from vendorguard.evidence.schema import KnowledgeEdition


def _edition(**overrides: object) -> KnowledgeEdition:
    """构造一份合法的法规版本快照, 仅覆盖当前测试所需字段。"""

    data: dict[str, object] = {
        "edition_key": "enterprise_information_publicity_regulation_v2024",
        "source_key": "state_council_regulations",
        "title": "企业信息公示暂行条例(2024 年修订)",
        "version": "2024-revised",
        "published_on": date(2024, 3, 18),
        "effective_from": date(2024, 5, 1),
        "effective_until": None,
        "snapshot_sha256": "a" * 64,
        "supersedes": (),
    }
    data.update(overrides)
    return KnowledgeEdition(**data)


def test_edition_uses_project_closed_effective_date_interval() -> None:
    """版本生效判定必须沿用项目“截止日当天仍有效”的既有口径。

    这条规则直接对应规则引擎的 ``valid_until`` 语义。检索层不能改用
    半开区间, 否则同一案件在规则评估和制度引用时会得到互相矛盾的结论。
    """

    edition = _edition(effective_until=date(2026, 9, 7))

    assert edition.is_effective_on(date(2024, 5, 1))
    assert edition.is_effective_on(date(2026, 9, 7))
    assert not edition.is_effective_on(date(2024, 4, 30))
    assert not edition.is_effective_on(date(2026, 9, 8))


def test_edition_rejects_invalid_snapshot_identity_and_date_range() -> None:
    """版本快照必须有可信哈希, 且生效区间和血缘不得自相矛盾。

    ``snapshot_sha256`` 是后续引用回放时验证正文未被覆盖的稳定锚点。
    自我替代, 重复前身和倒置的有效期都会令版本血缘无法可靠解释,
    因此在模型构造时立即拒绝。
    """

    with pytest.raises(ValidationError):
        _edition(snapshot_sha256="not-a-sha256")

    with pytest.raises(ValidationError):
        _edition(effective_until=date(2024, 4, 30))

    with pytest.raises(ValidationError):
        _edition(
            supersedes=("enterprise_information_publicity_regulation_v2024",),
        )

    with pytest.raises(ValidationError):
        _edition(supersedes=("v2014", "v2014"))


def test_edition_snapshot_is_immutable_after_creation() -> None:
    """已创建版本不得被原地改写, 修订必须创建新的版本快照。

    如果允许直接修改版本标题, 正文哈希或生效期, 历史案件保存的引用会
    指向变化后的内容, 审计记录就失去可复放性。
    """

    edition = _edition()

    with pytest.raises(ValidationError):
        edition.snapshot_sha256 = "b" * 64
