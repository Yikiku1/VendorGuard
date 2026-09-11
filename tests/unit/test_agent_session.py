"""审查会话跨轮状态的单元测试."""

from __future__ import annotations

import pytest

from vendorguard.agent_session import ReviewSession, SessionError, SupplementRecord
from vendorguard.materials import SourceTexts


def _session(max_rounds: int = 4) -> ReviewSession:
    return ReviewSession(sources=SourceTexts.from_materials([]), max_rounds=max_rounds)


def test_rounds_start_at_one_and_increment() -> None:
    """轮次从 1 开始递增, 供用户补充的来源定位使用."""

    session = _session()

    assert session.round_number == 0
    assert session.begin_round() == 1
    assert session.begin_round() == 2
    assert session.round_number == 2


def test_round_limit_is_enforced() -> None:
    """超过轮次上限时明确失败, 避免无界往复."""

    session = _session(max_rounds=2)
    session.begin_round()
    session.begin_round()

    with pytest.raises(SessionError) as caught:
        session.begin_round()

    assert "上限" in str(caught.value)


def test_supplement_requires_started_round() -> None:
    """未开始轮次前不能登记补充, 防止轮次归属错乱."""

    with pytest.raises(SessionError):
        _session().record_supplement("声明有效期至 2028-12-31")


def test_supplement_is_registered_as_program_controlled_source() -> None:
    """登记补充会同步进可核对来源, 轮次定位符由程序生成."""

    session = _session()
    session.begin_round()
    record = session.record_supplement("营业执照声明有效期至 2028-12-31")

    assert isinstance(record, SupplementRecord)
    assert record.as_locator() == "用户补充@R1"
    assert session.sources.supplements[1] == "营业执照声明有效期至 2028-12-31"


def test_supplement_message_marks_user_provided_origin() -> None:
    """补充写入历史时显式标注为用户提供, 不能伪装成材料原文."""

    session = _session()
    session.begin_round()
    record = session.record_supplement("有效期至 2028-12-31")

    message = record.as_message()
    assert "用户提供" in message
    assert "非材料原文" in message
    assert "用户补充@R1" in message


def test_history_lines_include_all_supplements_in_order() -> None:
    """历史按轮次顺序包含全部补充, 供下一轮继续引用."""

    session = _session()
    session.begin_round()
    session.record_supplement("第一条补充")
    session.begin_round()
    session.record_supplement("第二条补充")

    lines = session.history_lines()

    assert len(lines) == 2
    assert "第一条补充" in lines[0]
    assert "第二条补充" in lines[1]


def test_unregistered_round_locator_does_not_verify() -> None:
    """模型编造一个未登记的轮次时, 来源核对必然失败."""

    session = _session()
    session.begin_round()
    session.record_supplement("有效期至 2028-12-31")

    assert session.sources.text_for("用户补充@R9") is None
