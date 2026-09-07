"""知识来源模型的契约测试。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vendorguard.evidence.schema import KnowledgeSource


def test_external_source_requires_verifiable_provenance() -> None:
    """公开来源必须同时记录可访问地址和带时区的抓取时间。

    这条契约保证后续法规, 规章和商业资料能回放到确定的外部来源。
    没有抓取时间就无法判断快照何时取得; 没有时区则跨系统比较时会产生
    歧义。自制制度不在这条规则的范围内, 后续用独立测试覆盖。
    """

    valid_data = {
        "source_key": "state_council_regulations",
        "title": "国家行政法规库",
        "publisher": "司法部",
        "source_origin": "gov_document",
        "canonical_url": "https://xzfg.moj.gov.cn/",
        "retrieved_at": datetime(2026, 9, 7, tzinfo=UTC),
    }

    source = KnowledgeSource(**valid_data)

    assert source.source_key == "state_council_regulations"

    with pytest.raises(ValidationError):
        KnowledgeSource(**(valid_data | {"canonical_url": None}))

    with pytest.raises(ValidationError):
        KnowledgeSource(**(valid_data | {"retrieved_at": datetime(2026, 9, 7)}))
