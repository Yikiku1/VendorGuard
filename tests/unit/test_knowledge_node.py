"""KnowledgeNode 的版本化定位与正文完整性测试。"""

from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

from vendorguard.evidence.schema import KnowledgeNode


def _node_data(**overrides: object) -> dict[str, object]:
    """返回一个可回放的法规条文节点。"""

    body = "第十九条 政府部门应当建立信用约束机制."
    data: dict[str, object] = {
        "node_key": "article_19",
        "edition_key": "enterprise_information_publicity_regulation_v2024",
        "parent_node_key": "chapter_4",
        "node_type": "article",
        "locator": ("第四章", "第十九条"),
        "snapshot_sha256": "a" * 64,
        "normalized_sha256": "b" * 64,
        "char_start": 1280,
        "char_end": 1280 + len(body),
        "body": body,
        "body_sha256": sha256(body.encode("utf-8")).hexdigest(),
    }
    data.update(overrides)
    return data


def test_node_binds_edition_snapshot_normalized_text_and_locator() -> None:
    """最终引用同时绑定版本原件和规范文本中的精确位置。"""

    node = KnowledgeNode.model_validate(_node_data())

    assert node.edition_key == "enterprise_information_publicity_regulation_v2024"
    assert node.parent_node_key == "chapter_4"
    assert node.locator == ("第四章", "第十九条")
    assert node.char_start == 1280
    assert node.char_end == 1280 + len(node.body)
    assert node.body_sha256 == sha256(node.body.encode("utf-8")).hexdigest()


def test_node_is_immutable_after_creation() -> None:
    """节点创建后不得原地修改, 防止历史引用随重处理漂移。"""

    node = KnowledgeNode.model_validate(_node_data())

    with pytest.raises(ValidationError):
        node.body = "被替换的正文"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"body_sha256": "0" * 64}, "正文哈希"),
        ({"char_end": 1280}, "字符区间"),
        ({"char_end": 1280 + len("错误")}, "字符区间"),
        ({"parent_node_key": "article_19"}, "自身"),
        ({"locator": ("第四章", "  ")}, "定位符"),
        ({"snapshot_sha256": "not-a-sha256"}, "String should match pattern"),
    ],
)
def test_node_rejects_incoherent_locator_or_verbatim_metadata(
    overrides: dict[str, object], message: str
) -> None:
    """定位层级, 字符区间和正文哈希必须与节点正文一致。"""

    with pytest.raises(ValidationError, match=message):
        KnowledgeNode.model_validate(_node_data(**overrides))


def test_node_rejects_unknown_node_type() -> None:
    """节点类型必须来自受控集合, 供法规和表格解析统一处理。"""

    with pytest.raises(ValidationError):
        KnowledgeNode.model_validate(_node_data(node_type="sentence"))
