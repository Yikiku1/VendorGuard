"""维护一次审查会话的跨轮状态: 消息历史、用户补充与轮次上限.

M1 的 `run_check()` 每次构造全新消息并追问即结束; 这里需要"补充后在同一
会话继续"。单轮循环逻辑仍然不动, 跨轮状态集中在本模块。

用户补充的两条约束:

- 每轮补充的原文与轮次由程序登记, 模型只能按 `用户补充@R<轮次>` 引用。
  轮次由程序递增掌握, 因此模型无法伪造一个不存在的轮次。
- 补充内容在消息历史里显式标注为用户提供, 模型不能把它伪装成材料原文。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .materials import SourceTexts

DEFAULT_MAX_ROUNDS = 4


class SessionError(ValueError):
    """会话轮次越界或状态非法时抛出的错误。"""


class SupplementRecord(BaseModel):
    """一轮用户补充的原文与归属标记。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_number: int = Field(ge=1)
    text: str = Field(min_length=1)

    def as_locator(self) -> str:
        """该轮补充对应的来源定位符。"""

        return f"用户补充@R{self.round_number}"

    def as_message(self) -> str:
        """渲染为消息历史里的一行, 显式标注为用户提供。"""

        return (
            f"第 {self.round_number} 轮用户补充(由用户提供, 非材料原文, "
            f"引用时来源写作 {self.as_locator()}): {self.text}"
        )


class ReviewSession(BaseModel):
    """一次审查会话: 材料来源、补充记录与轮次计数。"""

    model_config = ConfigDict(extra="forbid")

    sources: SourceTexts
    supplements: list[SupplementRecord] = []
    max_rounds: int = DEFAULT_MAX_ROUNDS
    _round: int = 0

    @property
    def round_number(self) -> int:
        """已开始的轮次数, 第一轮从 1 开始。"""

        return self._round

    def begin_round(self) -> int:
        """开始新一轮; 超过上限时明确失败而不是无界往复。"""

        if self._round >= self.max_rounds:
            raise SessionError(f"会话轮次达到上限 {self.max_rounds} 轮")
        self._round += 1
        return self._round

    def record_supplement(self, text: str) -> SupplementRecord:
        """登记当前轮的用户补充, 并同步进可核对来源。"""

        if self._round < 1:
            raise SessionError("尚未开始任何轮次, 不能登记用户补充")
        record = SupplementRecord(round_number=self._round, text=text)
        self.supplements.append(record)
        self.sources.add_supplement(record.round_number, text)
        return record

    def history_lines(self) -> list[str]:
        """按轮次渲染用户补充, 供拼进消息历史。"""

        return [record.as_message() for record in self.supplements]


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "ReviewSession",
    "SessionError",
    "SupplementRecord",
]
