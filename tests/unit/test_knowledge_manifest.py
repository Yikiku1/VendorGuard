"""知识来源与版本关系的契约测试。"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from vendorguard.evidence.schema import (
    KnowledgeCorpusManifest,
    KnowledgeEdition,
    KnowledgeSource,
)


def test_manifest_accepts_closed_same_source_version_lineage() -> None:
    """同一来源下的历史版本和现行版本可以组成一份有效语料清单。

    2014 版在 2024-04-30 失效, 2024 版从次日生效并明确替代前者。
    这是后续按案件 as_of 日期检索并回放法规依据的最小数据形状。
    """

    source = KnowledgeSource(
        source_key="state_council_regulations",
        title="国家行政法规库",
        publisher="司法部",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/",
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    old_edition = KnowledgeEdition(
        edition_key="enterprise_information_publicity_regulation_v2014",
        source_key=source.source_key,
        title="企业信息公示暂行条例(2014 年原版)",
        version="2014-original",
        published_on=date(2014, 8, 7),
        effective_from=date(2014, 10, 1),
        effective_until=date(2024, 4, 30),
        snapshot_sha256="a" * 64,
    )
    current_edition = KnowledgeEdition(
        edition_key="enterprise_information_publicity_regulation_v2024",
        source_key=source.source_key,
        title="企业信息公示暂行条例(2024 年修订)",
        version="2024-revised",
        published_on=date(2024, 3, 18),
        effective_from=date(2024, 5, 1),
        snapshot_sha256="b" * 64,
        supersedes=(old_edition.edition_key,),
    )

    manifest = KnowledgeCorpusManifest(
        sources=(source,),
        editions=(old_edition, current_edition),
    )

    assert manifest.editions == (old_edition, current_edition)


def test_manifest_rejects_edition_with_missing_source() -> None:
    """版本必须指向清单内已声明的来源。

    版本快照若没有来源, 后续无法回放官方地址, 发布机关和抓取时间。
    因此不能等到入库或检索时才发现该引用已经失去可追溯性。
    """

    source = KnowledgeSource(
        source_key="state_council_regulations",
        title="国家行政法规库",
        publisher="司法部",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/",
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    orphan_edition = KnowledgeEdition(
        edition_key="enterprise_information_publicity_regulation_v2024",
        source_key="missing_source",
        title="企业信息公示暂行条例(2024 年修订)",
        version="2024-revised",
        published_on=date(2024, 3, 18),
        effective_from=date(2024, 5, 1),
        snapshot_sha256="a" * 64,
    )

    with pytest.raises(ValidationError):
        KnowledgeCorpusManifest(sources=(source,), editions=(orphan_edition,))


def test_manifest_rejects_edition_with_missing_predecessor() -> None:
    """版本替代关系必须指向同一清单中已保存的历史版本。

    若前身不存在, 就无法证明新版本到底替代了哪份正文快照。历史案件
    也无法据此回放应使用的制度版本, 所以必须在构造清单时拒绝。
    """

    source = KnowledgeSource(
        source_key="state_council_regulations",
        title="国家行政法规库",
        publisher="司法部",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/",
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    current_edition = KnowledgeEdition(
        edition_key="enterprise_information_publicity_regulation_v2024",
        source_key=source.source_key,
        title="企业信息公示暂行条例(2024 年修订)",
        version="2024-revised",
        published_on=date(2024, 3, 18),
        effective_from=date(2024, 5, 1),
        snapshot_sha256="a" * 64,
        supersedes=("enterprise_information_publicity_regulation_v2014",),
    )

    with pytest.raises(ValidationError):
        KnowledgeCorpusManifest(sources=(source,), editions=(current_edition,))


def test_manifest_rejects_cross_source_version_lineage() -> None:
    """版本只能替代同一来源下的历史版本。

    内部供应商准入制度与外部行政法规可以共同存在于语料库, 但它们
    不是同一份文书的修订关系。允许跨来源替代会破坏版本链的法律含义。
    """

    external_source = KnowledgeSource(
        source_key="state_council_regulations",
        title="国家行政法规库",
        publisher="司法部",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/",
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    internal_source = KnowledgeSource(
        source_key="vendorguard_internal_policy",
        title="VendorGuard 演示制度",
        publisher="VendorGuard",
        source_origin="synthetic",
    )
    external_edition = KnowledgeEdition(
        edition_key="enterprise_information_publicity_regulation_v2014",
        source_key=external_source.source_key,
        title="企业信息公示暂行条例(2014 年原版)",
        version="2014-original",
        published_on=date(2014, 8, 7),
        effective_from=date(2014, 10, 1),
        effective_until=date(2024, 4, 30),
        snapshot_sha256="a" * 64,
    )
    internal_edition = KnowledgeEdition(
        edition_key="demo_supplier_admission_policy_v2",
        source_key=internal_source.source_key,
        title="供应商准入管理办法(演示现行)",
        version="2.0",
        published_on=date(2026, 1, 1),
        effective_from=date(2026, 1, 1),
        snapshot_sha256="b" * 64,
        supersedes=(external_edition.edition_key,),
    )

    with pytest.raises(ValidationError):
        KnowledgeCorpusManifest(
            sources=(external_source, internal_source),
            editions=(external_edition, internal_edition),
        )


def test_manifest_rejects_duplicate_source_and_edition_keys() -> None:
    """来源键和版本键分别是清单内的稳定唯一标识。

    重复来源键会令某个版本无法确定发布方与抓取地址。重复版本键会令
    历史引用无法确定正文快照, 因此两种冲突都必须在构造清单时拒绝。
    """

    source = KnowledgeSource(
        source_key="state_council_regulations",
        title="国家行政法规库",
        publisher="司法部",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/",
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    duplicate_source = KnowledgeSource(
        source_key=source.source_key,
        title="国家行政法规库镜像",
        publisher="司法部",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/mobile/",
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    edition = KnowledgeEdition(
        edition_key="enterprise_information_publicity_regulation_v2024",
        source_key=source.source_key,
        title="企业信息公示暂行条例(2024 年修订)",
        version="2024-revised",
        published_on=date(2024, 3, 18),
        effective_from=date(2024, 5, 1),
        snapshot_sha256="a" * 64,
    )
    duplicate_edition = KnowledgeEdition(
        edition_key=edition.edition_key,
        source_key=source.source_key,
        title="企业信息公示暂行条例(错误重复快照)",
        version="2024-revised-copy",
        published_on=date(2024, 3, 18),
        effective_from=date(2024, 5, 1),
        snapshot_sha256="b" * 64,
    )

    with pytest.raises(ValidationError):
        KnowledgeCorpusManifest(sources=(source, duplicate_source), editions=())

    with pytest.raises(ValidationError):
        KnowledgeCorpusManifest(sources=(source,), editions=(edition, duplicate_edition))
