"""冻结 VendorGuard RAG 一期语料的原始版本快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "data" / "knowledge"
LEGACY_POLICY_DIR = KNOWLEDGE_DIR / "policies"
RESEARCH_SOURCE_DIR = KNOWLEDGE_DIR / "sources"
RESEARCH_CATALOG_PATH = KNOWLEDGE_DIR / "manifests" / "source_catalog.json"
SNAPSHOT_DIR = KNOWLEDGE_DIR / "snapshots"
CORPUS_MANIFEST_PATH = KNOWLEDGE_DIR / "manifests" / "corpus_v1.json"

SourceOrigin = Literal["synthetic", "gov_document"]


@dataclass(frozen=True)
class EditionSpec:
    """描述一期一个不可变版本需要冻结的来源事实。"""

    edition_key: str
    source_key: str
    title: str
    publisher: str
    source_origin: SourceOrigin
    canonical_url: str | None
    version: str
    published_on: str
    effective_from: str
    effective_until: str | None
    supersedes: tuple[str, ...]
    authority: str
    role: str
    applicability: str
    existing_source_key: str | None = None
    legacy_policy_key: str | None = None


EDITION_SPECS: tuple[EditionSpec, ...] = (
    EditionSpec(
        edition_key="demo_supplier_admission_policy_v1",
        source_key="vendorguard_internal_admission_policy",
        title="供应商准入管理办法(演示, 已废止)",
        publisher="VendorGuard 演示制度库",
        source_origin="synthetic",
        canonical_url=None,
        version="1.0",
        published_on="2023-01-01",
        effective_from="2023-01-01",
        effective_until="2025-12-31",
        supersedes=(),
        authority="internal",
        role="version_negative",
        applicability="仅用于历史案件版本回放, 不得作为 2026-01-01 后准入依据。",
        legacy_policy_key="demo_supplier_admission_policy_v1",
    ),
    EditionSpec(
        edition_key="demo_supplier_admission_policy_v2",
        source_key="vendorguard_internal_admission_policy",
        title="供应商准入管理办法(演示, 现行)",
        publisher="VendorGuard 演示制度库",
        source_origin="synthetic",
        canonical_url=None,
        version="2.0",
        published_on="2026-01-01",
        effective_from="2026-01-01",
        effective_until=None,
        supersedes=("demo_supplier_admission_policy_v1",),
        authority="internal",
        role="direct_policy",
        applicability="VEN-001 的直接制度依据, 仅适用于供应商准入材料核验。",
        legacy_policy_key="demo_supplier_admission_policy_v2",
    ),
    EditionSpec(
        edition_key="demo_supplier_required_documents_policy_v3",
        source_key="vendorguard_internal_required_documents_policy",
        title="分品类必需准入材料清单(演示, 现行)",
        publisher="VendorGuard 演示制度库",
        source_origin="synthetic",
        canonical_url=None,
        version="3.0",
        published_on="2026-01-01",
        effective_from="2026-01-01",
        effective_until=None,
        supersedes=(),
        authority="internal",
        role="direct_policy",
        applicability="VEN-002 的唯一直接制度依据, 表格行将作为材料要求的引用单位。",
        legacy_policy_key="demo_supplier_required_documents_policy_v3",
    ),
    EditionSpec(
        edition_key="market_entity_registration_regulation_v2021",
        source_key="state_council_regulations",
        title="中华人民共和国市场主体登记管理条例",
        publisher="国务院",
        source_origin="gov_document",
        canonical_url="https://www.mee.gov.cn/zcwj/gwywj/202108/t20210824_860263.shtml",
        version="国务院令第746号",
        published_on="2021-07-27",
        effective_from="2022-03-01",
        effective_until=None,
        supersedes=(),
        authority="administrative_regulation",
        role="background",
        applicability="登记事项和营业执照效力的背景, 不构成企业供应商准入义务。",
    ),
    EditionSpec(
        edition_key="market_entity_registration_rules_v2022",
        source_key="state_administration_market_regulation",
        title="市场主体登记管理条例实施细则",
        publisher="国家市场监督管理总局",
        source_origin="gov_document",
        canonical_url="https://www.gov.cn/zhengce/zhengceku/2022-03/02/content_5676403.htm",
        version="国家市场监督管理总局令第52号",
        published_on="2022-03-01",
        effective_from="2022-03-01",
        effective_until=None,
        supersedes=(),
        authority="department_rule",
        role="background",
        applicability="营业执照记载事项和效力的背景, 不构成企业供应商准入义务。",
    ),
    EditionSpec(
        edition_key="enterprise_information_publicity_regulation_v2014",
        source_key="state_council_regulations",
        title="企业信息公示暂行条例",
        publisher="国务院",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/front/law/detail?LawID=415",
        version="国务院令第654号",
        published_on="2014-08-07",
        effective_from="2014-10-01",
        effective_until="2024-04-30",
        supersedes=(),
        authority="administrative_regulation",
        role="version_negative",
        applicability="只用于历史版本过滤和引用定位测试, 不构成企业准入制度。",
    ),
    EditionSpec(
        edition_key="enterprise_information_publicity_regulation_v2024",
        source_key="state_council_regulations",
        title="企业信息公示暂行条例",
        publisher="国务院",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/front/law/detail?LawID=1718",
        version="国务院令第777号修订文本",
        published_on="2024-03-10",
        effective_from="2024-05-01",
        effective_until=None,
        supersedes=("enterprise_information_publicity_regulation_v2014",),
        authority="administrative_regulation",
        role="background_and_version_negative",
        applicability="企业信息公示背景和版本过滤测试, 政府部门义务不得转化为企业准入义务。",
        existing_source_key="enterprise_information_publicity_regulation_2024_moj",
    ),
    EditionSpec(
        edition_key="certification_and_accreditation_regulation_v2023",
        source_key="state_council_certification_regulations",
        title="中华人民共和国认证认可条例",
        publisher="国务院",
        source_origin="gov_document",
        canonical_url="https://xzfg.moj.gov.cn/front/law/detail?LawID=1688",
        version="2023年第三次修订文本",
        published_on="2023-07-20",
        effective_from="2023-07-20",
        effective_until=None,
        supersedes=(),
        authority="administrative_regulation",
        role="conditional_reference",
        applicability="仅在案件已证明产品适用认证目录或指定认证类型时提供背景。",
        existing_source_key="certification_accreditation_regulation_2023_moj",
    ),
    EditionSpec(
        edition_key="compulsory_product_certification_rules_v2022",
        source_key="state_administration_market_regulation",
        title="强制性产品认证管理规定",
        publisher="国家市场监督管理总局",
        source_origin="gov_document",
        canonical_url="https://isccc.gov.cn/xxgk1/zcfg_3/bmgz/202507/t20250725_8567.htm",
        version="2022年修订文本",
        published_on="2022-09-29",
        effective_from="2022-09-29",
        effective_until=None,
        supersedes=(),
        authority="department_rule",
        role="conditional_reference",
        applicability="仅在案件事实证明产品位于 CCC 目录时提供背景, 不产生通用材料要求。",
        existing_source_key="compulsory_product_certification_rules_2025_isccc",
    ),
    EditionSpec(
        edition_key="product_quality_law_v2018",
        source_key="national_peoples_congress_laws",
        title="中华人民共和国产品质量法",
        publisher="全国人民代表大会常务委员会",
        source_origin="gov_document",
        canonical_url="https://www.cnipa.gov.cn/art/2019/7/31/art_104_67810.html",
        version="2018年第三次修正后文本",
        published_on="2018-12-29",
        effective_from="2018-12-29",
        effective_until=None,
        supersedes=(),
        authority="law",
        role="version_negative",
        applicability="质量认证自愿原则的对抗负例, 不得反向推导内部材料清单。",
        existing_source_key="product_quality_law_cnipa",
    ),
)


def _sha256(content: bytes) -> str:
    """返回原始字节的 SHA-256 小写十六进制摘要。"""

    return hashlib.sha256(content).hexdigest()


def _read_research_catalog() -> dict[str, dict[str, object]]:
    """读取既有研究采集清单, 用于复核复用的原始响应。"""

    raw_catalog = json.loads(RESEARCH_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw_catalog, list):
        raise ValueError("研究来源清单必须是列表")

    catalog: dict[str, dict[str, object]] = {}
    for item in raw_catalog:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ValueError("研究来源清单包含无效条目")
        catalog[item["key"]] = item
    return catalog


def _render_internal_policy(spec: EditionSpec) -> bytes:
    """将已确认的自制制度迁入首次冻结的 Markdown 原件。"""

    if spec.legacy_policy_key is None:
        raise ValueError(f"{spec.edition_key} 未配置内部制度来源")

    path = LEGACY_POLICY_DIR / f"{spec.legacy_policy_key}.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"内部制度不是映射: {path}")

    sections = payload.get("sections")
    if not isinstance(sections, list):
        raise ValueError(f"内部制度缺少 sections: {path}")

    lines = [f"# {spec.title}", ""]
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError(f"内部制度包含无效章节: {path}")
        title = section.get("title")
        body = section.get("body")
        if not isinstance(title, str) or not isinstance(body, str):
            raise ValueError(f"内部制度章节缺少标题或正文: {path}")
        lines.extend((f"## {title}", "", body.rstrip(), ""))

    return "\n".join(lines).encode("utf-8")


def _load_existing_source(
    spec: EditionSpec, catalog: dict[str, dict[str, object]]
) -> tuple[bytes, dict[str, object]]:
    """复用并校验已有研究阶段的官方原始响应。"""

    if spec.existing_source_key is None:
        raise ValueError(f"{spec.edition_key} 未配置既有原始响应")
    item = catalog.get(spec.existing_source_key)
    if item is None:
        raise ValueError(f"研究来源清单缺少: {spec.existing_source_key}")

    source_file = item.get("file")
    listed_hash = item.get("sha256")
    if not isinstance(source_file, str) or not isinstance(listed_hash, str):
        raise ValueError(f"研究来源清单缺少文件或哈希: {spec.existing_source_key}")

    content = (KNOWLEDGE_DIR / source_file).read_bytes()
    if _sha256(content) != listed_hash:
        raise ValueError(f"研究来源原始响应哈希不匹配: {spec.existing_source_key}")

    return content, {
        "retrieved_at": item.get("retrieved_at"),
        "http_status": item.get("http_status"),
        "content_type": item.get("content_type"),
        "acquisition_method": "copied_from_verified_research_snapshot",
    }


def _download_source(spec: EditionSpec) -> tuple[bytes, dict[str, object]]:
    """下载缺失的官方原始响应, 不对正文做清洗或转码。"""

    if spec.canonical_url is None:
        raise ValueError(f"{spec.edition_key} 缺少官方来源 URL")

    request = Request(
        spec.canonical_url,
        headers={"User-Agent": "VendorGuard-RAG-Corpus/1.0"},
    )
    with urlopen(request, timeout=60) as response:
        content = response.read()
        return content, {
            "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "http_status": response.status,
            "content_type": response.headers.get_content_type(),
            "acquisition_method": "downloaded_from_canonical_url",
        }


def _source_file_name(spec: EditionSpec) -> str:
    """根据来源类型选择不改变原始格式的快照文件名。"""

    return "original.md" if spec.source_origin == "synthetic" else "original.html"


def _build_snapshot(
    spec: EditionSpec, catalog: dict[str, dict[str, object]]
) -> tuple[bytes, dict[str, object]]:
    """构建一个版本的原始内容与采集元数据。"""

    if spec.source_origin == "synthetic":
        content = _render_internal_policy(spec)
        metadata: dict[str, object] = {
            "retrieved_at": None,
            "http_status": None,
            "content_type": "text/markdown; charset=utf-8",
            "acquisition_method": "frozen_from_confirmed_internal_policy",
        }
    elif spec.existing_source_key is not None:
        content, metadata = _load_existing_source(spec, catalog)
    else:
        content, metadata = _download_source(spec)

    if not content:
        raise ValueError(f"原始响应为空: {spec.edition_key}")
    return content, metadata


def _build_manifest_entry(
    spec: EditionSpec,
    source_file_name: str,
    content: bytes,
    metadata: dict[str, object],
) -> dict[str, object]:
    """构建绑定原始文件哈希的正式版本登记。"""

    return {
        "edition_key": spec.edition_key,
        "source_key": spec.source_key,
        "title": spec.title,
        "publisher": spec.publisher,
        "source_origin": spec.source_origin,
        "canonical_url": spec.canonical_url,
        "version": spec.version,
        "published_on": spec.published_on,
        "effective_from": spec.effective_from,
        "effective_until": spec.effective_until,
        "supersedes": list(spec.supersedes),
        "authority": spec.authority,
        "role": spec.role,
        "applicability": spec.applicability,
        "snapshot_path": f"snapshots/{spec.edition_key}/{source_file_name}",
        "snapshot_sha256": _sha256(content),
        **metadata,
    }


def _write_json(path: Path, payload: object) -> None:
    """写入带稳定缩进的 UTF-8 JSON。"""

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def freeze_corpus() -> None:
    """下载或复核全部来源后, 一次性创建不可覆盖的一期快照目录。"""

    if SNAPSHOT_DIR.exists() or CORPUS_MANIFEST_PATH.exists():
        raise FileExistsError("正式快照或 corpus_v1.json 已存在, 冻结后不得原地覆盖")

    catalog = _read_research_catalog()
    snapshots: list[tuple[EditionSpec, bytes, dict[str, object]]] = []
    for spec in EDITION_SPECS:
        content, metadata = _build_snapshot(spec, catalog)
        snapshots.append((spec, content, metadata))

    source_keys = {spec.source_key for spec in EDITION_SPECS}
    if len({spec.edition_key for spec in EDITION_SPECS}) != len(EDITION_SPECS):
        raise ValueError("一期版本键不能重复")

    manifest_entries = [
        _build_manifest_entry(spec, _source_file_name(spec), content, metadata)
        for spec, content, metadata in snapshots
    ]
    manifest = {
        "schema_version": "1.0",
        "corpus_key": "vendorguard_rag_phase_1",
        "edition_count": len(manifest_entries),
        "source_count": len(source_keys),
        "entries": manifest_entries,
    }

    SNAPSHOT_DIR.parent.mkdir(parents=True, exist_ok=True)
    CORPUS_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SNAPSHOT_DIR.parent) as temporary_dir:
        temporary_root = Path(temporary_dir)
        temporary_snapshots = temporary_root / "snapshots"
        temporary_snapshots.mkdir()

        for spec, content, metadata in snapshots:
            edition_dir = temporary_snapshots / spec.edition_key
            edition_dir.mkdir()
            source_file_name = _source_file_name(spec)
            (edition_dir / source_file_name).write_bytes(content)
            _write_json(
                edition_dir / "acquisition.json",
                _build_manifest_entry(spec, source_file_name, content, metadata),
            )

        temporary_manifest = temporary_root / "corpus_v1.json"
        _write_json(temporary_manifest, manifest)
        shutil.move(str(temporary_snapshots), str(SNAPSHOT_DIR))
        shutil.move(str(temporary_manifest), str(CORPUS_MANIFEST_PATH))

    print(f"冻结 {len(manifest_entries)} 个版本快照: {SNAPSHOT_DIR}")
    print(f"正式语料清单: {CORPUS_MANIFEST_PATH}")


def main() -> int:
    """解析命令行参数并运行冻结流程。"""

    parser = argparse.ArgumentParser(description="冻结 VendorGuard RAG 一期原始语料快照")
    parser.parse_args()

    try:
        freeze_corpus()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"冻结失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
