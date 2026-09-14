"""审查会话的单元测试.

契约由本测试固定: agent_session 只做一件事——持有当前材料和用户补充原文,
并把它们组装成可核对的来源。轮次号由程序递增, 补充原文原样保留, 空补充不登记。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vendorguard.agent_session import ReviewSession
from vendorguard.materials import MaterialDocument, read_text_pdf

MATERIALS_DIR = Path(__file__).resolve().parents[2] / "data" / "demo" / "materials"


@pytest.fixture
def material() -> MaterialDocument:
    """读一份固定样例当会话材料, 不再另造夹具。"""

    return read_text_pdf(MATERIALS_DIR / "license_missing_date.pdf")


def test_session_starts_with_material_only(material: MaterialDocument) -> None:
    """会话开启时只有材料, 补充轮次为 0, 来源里只有材料页。"""

    session = ReviewSession.start(material)
    sources = session.sources()

    assert session.material.material_id == "license_missing_date"
    assert session.supplement_rounds == 0
    assert sources.text_for("license_missing_date@page:2") is not None
    assert sources.text_for("user_supplement@round:1") is None


def test_record_supplement_keeps_text_and_numbers_rounds(material: MaterialDocument) -> None:
    """补充原文原样保留, 轮次从 1 递增, 且立刻成为可核对来源。"""

    session = ReviewSession.start(material)

    first = session.record_supplement("营业执照有效期至 2028年05月20日")
    second = session.record_supplement(" 另外, 材料清单状态: 齐全 ")

    assert (first, second) == (1, 2)
    assert session.supplement_rounds == 2
    sources = session.sources()
    assert sources.text_for("user_supplement@round:1") == "营业执照有效期至 2028年05月20日"
    # 前后空白不裁剪: 核对用的必须是用户原话
    assert sources.text_for("user_supplement@round:2") == " 另外, 材料清单状态: 齐全 "
    assert sources.text_for("user_supplement@round:3") is None
    assert sources.contains_date("user_supplement@round:1", date(2028, 5, 20)) is True


def test_empty_supplement_is_rejected(material: MaterialDocument) -> None:
    """空补充不登记, 否则会出现一条看似存在、其实无内容的来源。"""

    session = ReviewSession.start(material)

    with pytest.raises(ValueError, match="不能为空"):
        session.record_supplement("   ")

    assert session.supplement_rounds == 0


def test_supplement_and_material_locators_do_not_mix(material: MaterialDocument) -> None:
    """两类来源互不通用: 补充不能被当成材料页, 材料页也不能被当成补充轮次。"""

    session = ReviewSession.start(material)
    session.record_supplement("营业执照有效期至 2028年05月20日")
    sources = session.sources()

    assert sources.text_for("user_supplement@page:1") is None
    assert sources.text_for("license_missing_date@round:1") is None
