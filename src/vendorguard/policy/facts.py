"""定义准入评估所需的结构化模拟事实及其来源定位契约。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

# 启用规则 VEN-001/VEN-002 消费的事实字段白名单, 顺序用于确定性报错。
_KNOWN_FACT_FIELDS = (
    "business_license_document_status",
    "category_required_documents_complete",
)

BusinessLicenseDocumentStatus = Literal["valid", "expired", "inconsistent", "unreadable"]


class FactsValidationError(ValueError):
    """结构化事实缺失、字段越界或来源定位非法时抛出的错误。"""


class StructuredFacts(BaseModel):
    """保存本期启用规则需要的模拟事实及每个事实的来源定位。"""

    model_config = ConfigDict(extra="forbid")

    business_license_document_status: BusinessLicenseDocumentStatus | None = None
    category_required_documents_complete: StrictBool | None = None
    sources: dict[str, str] = Field(default_factory=dict)

    def __init__(self, **data: object) -> None:
        """构造事实对象, 把 Pydantic 校验错误统一转换成 FactsValidationError。"""

        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise FactsValidationError(str(exc)) from exc

    @model_validator(mode="after")
    def validate_source_locations(self) -> StructuredFacts:
        """每个已给出的事实都必须带合法来源定位, 来源也不能引用未知事实。"""

        for field in _KNOWN_FACT_FIELDS:
            if getattr(self, field) is None:
                continue
            location = self.sources.get(field)
            if location is None:
                raise ValueError(f"事实 {field} 缺少来源定位")
            document_id, separator, locator = location.partition("@")
            if not separator or not document_id or not locator:
                raise ValueError(f"事实 {field} 的来源定位格式非法: {location}")

        for key in self.sources:
            if key not in _KNOWN_FACT_FIELDS:
                raise ValueError(f"来源定位引用了未知事实: {key}")
        return self


def certificate_remaining_days(
    *,
    valid_until: date | None,
    reference_date: date,
) -> int:
    """计算有效期剩余天数; 到期日当天仍视为有效。"""

    if valid_until is None:
        raise FactsValidationError("无法计算剩余天数: 缺少 valid_until")
    return (valid_until - reference_date).days


def load_demo_case_facts(
    path: str | Path,
    *,
    stage: str | None = None,
) -> StructuredFacts:
    """从演示案例 YAML 读取本期启用规则相关的事实与来源定位。

    正常案事实放在顶层 structured_facts, 来源同段内嵌; 补件案把不同阶段的
    事实放在 initial_submission 或 after_supplement 子段, 来源为该子段的
    sources。加载器只挑白名单内且该阶段确实声明的字段, 缺失字段保持缺省,
    不代为猜测。
    """

    case_path = Path(path)
    try:
        raw_text = case_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise FactsValidationError(f"无法读取演示案例文件: {case_path}") from exc

    section = raw[stage] if stage is not None else raw
    block = section.get("structured_facts", {})

    sources = block.get("sources")
    if not isinstance(sources, dict):
        sources = section.get("sources", {})

    data: dict[str, object] = {
        field: block[field] for field in _KNOWN_FACT_FIELDS if field in block
    }
    data["sources"] = {key: value for key, value in sources.items() if key in data}

    return StructuredFacts(**data)


__all__ = [
    "FactsValidationError",
    "StructuredFacts",
    "certificate_remaining_days",
    "load_demo_case_facts",
]
