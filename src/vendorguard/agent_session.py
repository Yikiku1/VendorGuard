"""一次材料审查的会话: 当前材料, 用户补充原文与轮次.

会话由程序持有: 材料是命令行读出来的那一份, 补充文本是用户在追问后真实
输入的原文. 模型只能引用已登记的轮次, 造不出一轮不存在的补充, 也改不了
补充的原文 (M2 方案 3.3).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .materials import MaterialDocument, MaterialSources


class ReviewSession(BaseModel):
    """一次审查会话的状态: 一份材料 + 已登记的用户补充.

    材料 ID 与补充轮次都由程序掌握, 所以来源定位里的
    `材料ID@page:N` 与 `user_supplement@round:N` 没有可被伪造的部分.
    """

    model_config = ConfigDict(extra="forbid")

    material: MaterialDocument
    supplements: list[str] = Field(default_factory=list)

    @classmethod
    def start(cls, material: MaterialDocument) -> ReviewSession:
        """用一份已登记材料开启会话; 此时还没有任何补充."""

        return cls(material=material)

    @property
    def supplement_rounds(self) -> int:
        """已登记的用户补充轮次, 一条补充算一轮."""

        return len(self.supplements)

    def record_supplement(self, text: str) -> int:
        """登记一轮用户补充的原文, 返回轮次号 (从 1 开始).

        原文原样保存, 不摘要也不改写: 核对时用的必须是用户说过的话.
        空文本拒绝登记——否则"补充了但没内容"会变成一条看似存在的来源.
        """

        if not text.strip():
            raise ValueError("用户补充不能为空")
        self.supplements.append(text)
        return len(self.supplements)

    def sources(self) -> MaterialSources:
        """把当前材料与已登记补充组装成可核对的来源集合."""

        sources = MaterialSources.from_materials([self.material])
        for round_number, text in enumerate(self.supplements, start=1):
            sources.add_supplement(round_number, text)
        return sources


__all__ = ["ReviewSession"]
