# VendorGuard RAG 一期官方语料来源核验

> 文档类型: 官方一手来源核验记录
>
> 核验日期: 2026-09-07
>
> 范围: 一期候选的外部法规来源。本文不新增或改写 `data/knowledge/` 中的语料, 只确定后续本地不可变快照应从哪里取得以及其版本边界。

## 结论

一期外部法规可以作为登记、公示、认证和产品质量的背景或条件性参考, 不能替代公司内部准入制度。`VEN-001` 的直接规则仍是内部《供应商准入管理办法》, `VEN-002` 的直接规则仍是内部《分品类必需准入材料清单》。

现有仓库已保存 2024 年《企业信息公示暂行条例》、2023 年《中华人民共和国认证认可条例》、《强制性产品认证管理规定》和《中华人民共和国产品质量法》的官方网页抓取件。2014 原版《企业信息公示暂行条例》、`市场主体登记管理条例` 及其实施细则尚未有本地原始快照, 不能声称已经具备完整回放能力。

国家行政法规库的同一法规会提供“历史沿革”版本页。因此 2014 原版和 2024 修订版必须分别下载、分别计算 SHA-256 并建立两个 `KnowledgeEdition`, 不能只在一个现行网页快照上标记不同 `doc_version`。

## 来源总表

下表的“可取状态”是本次对链接的 HTTP `HEAD` 或正文页核验结果。HTTP `200` 只说明本次可访问, 不替代抓取后的字节哈希。

| 文档和版本 | 官方一手来源 | 版本事实 | 可取状态 | 本地不可变快照建议 |
| --- | --- | --- | --- | --- |
| 《企业信息公示暂行条例》2014 原版, 国务院令第 654 号 | [国家行政法规库历史版本页](https://xzfg.moj.gov.cn/front/law/detail?LawID=415) | 2014-07-23 国务院常务会议通过, 2014-08-07 公布, 2014-10-01 施行。该页正文与 2024 版的主管机关表述不同, 是独立版本。 | 2026-09-07 HTTP `200`, `Content-Type: text/html; charset=UTF-8`, 响应 41,933 字节。仓库尚无该版本抓取件。 | 优先保存完整原始 HTML 响应, 记 SHA-256、抓取时间、URL、HTTP 状态、Content-Type 和字节数。页面有下载界面文字, 但本轮未核验到可直接固定的文件下载 URL, 故不将未下载的 Word/PDF 写为已取得原件。建议 `edition_key = enterprise_information_publicity_regulation_v2014`, `effective_from = 2014-10-01`, `effective_until = 2024-04-30`。 |
| 《企业信息公示暂行条例》2024 修订版 | [国家行政法规库现行版本页](https://xzfg.moj.gov.cn/front/law/detail?LawID=1718), [司法部修订解读](https://www.moj.gov.cn/pub/sfbgw/zcjd/202403/t20240329_496773.html) | 行政法规库记载依据 2024-03-10 《国务院关于修改和废止部分行政法规的决定》修订。`2024-05-01` 的适用起点应在入库时同修订决定原件再次核对。 | 2026-09-07 HTTP `200`, `Content-Type: text/html; charset=UTF-8`, 响应 36,736 字节。已有 [enterprise_information_publicity_regulation_2024_moj.html](../data/knowledge/sources/enterprise_information_publicity_regulation_2024_moj.html), 其目录记录 SHA-256 为 `3ee0dd82f214501d297cbe9813ff497626c359c71896e9990f7905529f0551c1`。 | 该现有 HTML 可保留为证据源, 并应同 2014 版保持相同快照策略。建议 `edition_key = enterprise_information_publicity_regulation_v2024`, `supersedes = enterprise_information_publicity_regulation_v2014`; 在修订决定原件复核前, 不固化 `effective_from`。 |
| 《中华人民共和国市场主体登记管理条例》, 国务院令第 746 号 | [生态环境部转载的中国政府网原文](https://www.mee.gov.cn/zcwj/gwywj/202108/t20210824_860263.shtml) | 国务院令第 746 号记载: 2021-04-14 国务院常务会议通过, 2021-07-27 公布, 2022-03-01 施行。第二十二条确认营业执照正、副本具有同等法律效力。 | 2026-09-07 正文请求成功。仓库尚无本地抓取件。 | 先保存该官方转载页的原始 HTML, 并在元数据保留“来源: 中国政府网”的页内标识; 后续若取得国务院公报 PDF, 将公报 PDF 作为更高优先级源并重建版本。建议 `edition_key = market_entity_registration_regulation_v2021`, `effective_from = 2022-03-01`。 |
| 《市场主体登记管理条例实施细则》, 市场监管总局令第 52 号 | [中国政府网政策库正文](https://www.gov.cn/zhengce/zhengceku/2022-03/02/content_5676403.htm), [司法部部门规章正文](https://www.moj.gov.cn/pub/sfbgw/flfggz/flfggzbmgz/202305/t20230510_478549.html) | 两页正文均为该实施细则。中国政府网页面包含总局令第 52 号和“自公布之日起施行”, 司法部页标注 2022-03-01 发布。第二十三条列明营业执照记载事项及电子、纸质营业执照的效力。 | 中国政府网于 2026-09-07 经命令行获取 HTTP `200`; 司法部页面在当前命令行客户端发生循环重定向, 但浏览器可正常读取。 | 已冻结中国政府网原始 HTML, 记录抓取时间和 SHA-256。建议 `edition_key = market_entity_registration_rules_v2022`, `effective_from = 2022-03-01`。 |
| 《中华人民共和国认证认可条例》2023 年第三次修订版 | [国家行政法规库](https://xzfg.moj.gov.cn/front/law/detail?LawID=1688) | 页面历史沿革列出 2003-09-03 公布、2016-02-06 第一次修订、2020-11-29 第二次修订、2023-07-20 第三次修订; 原条例自 2003-11-01 施行。 | 2026-09-07 HTTP `200`; 页面提供 Word/PDF。已有 [certification_accreditation_regulation_2023_moj.html](../data/knowledge/sources/certification_accreditation_regulation_2023_moj.html), 目录记录 SHA-256 为 `51904f527764b8d2669894e83ccf713c27f05026f8c7b4492c792acab5a98ef3`。 | 现有 HTML 可保留, 正式语料应新增下载原件并固定哈希。重要: 若此对象的版本键确实是“2023 第三次修订版”, `effective_from` 应按 2023 修订的适用日期建模, 不能误用 2003-11-01 作为该版本的开始日期; 需同时补录其 2020 前身或明确该版本仅支持 2023-07-20 后的回放。 |
| 《强制性产品认证管理规定》, 2009 公布、2022 修订 | [中国网络安全审查认证和市场监管大数据中心法规页](https://isccc.gov.cn/xxgk1/zcfg_3/bmgz/202507/t20250725_8567.htm) | 页面正文记载 2009-07-03 原国家质检总局令第 117 号公布, 2022-09-29 国家市场监管总局令第 61 号修订。它规定目录内产品须经认证, 不等于所有供应商均须提交认证材料。 | 2026-09-07 HTTP `200`; 已有 [compulsory_product_certification_rules_2025_isccc.html](../data/knowledge/sources/compulsory_product_certification_rules_2025_isccc.html), 目录记录 SHA-256 为 `4372cf2c97c26c6fde3c7769b714fca9cf0a976f40ba376f32a435bf8ce849d1`。 | 该页面是可复现正文来源, 但发布方不是规章制定机关。生产入库前应补取市场监管总局令第 61 号或法规库的第一方原件; 在取得前, 将本页标为“官方机构转载, 待发行机关原件核验”。当前合并文本不可写成自 2009 起一直有效的单一版本, 应以 2022 修订决定的实际施行日建立现行版本边界。 |
| 《中华人民共和国产品质量法》, 2018 年第三次修正后文本 | [国家知识产权局法规页](https://www.cnipa.gov.cn/art/2019/7/31/art_104_67810.html) | 页面载明 1993-02-22 通过, 2000、2009 两次修正, 2018-12-29 根据全国人大常委会决定第三次修正; 法律原始施行日为 1993-09-01。第十四条规定质量体系认证和产品质量认证可依自愿原则申请。 | 2026-09-07 HTTP `200`; 已有 [product_quality_law_cnipa.html](../data/knowledge/sources/product_quality_law_cnipa.html), 目录记录 SHA-256 为 `035972832a37c4d536d493c6710994acc2d20b787c6f9865e990d9563f62ab4c`。 | 保留现有 HTML 作为来源证据; 正式入库时优先补取全国人大法律数据库或全国人大常委会公报的 2018 修正决定及现行文本。若以“2018 第三次修正后文本”建独立版本, 不应把 `effective_from` 填为 1993-09-01。 |

## 对现有本地资料的核验结果

已存在的抓取件均由 `data/knowledge/manifests/source_catalog.json` 管理, 清单保留了 URL、抓取时间、HTTP 状态、文件字节数和 SHA-256。这是正确的来源溯证起点, 但其中部分政策 YAML 是节选文本, 不能替代全量原始快照。

| 已有文件 | 对应来源 | 当前用途判断 |
| --- | --- | --- |
| `data/knowledge/sources/enterprise_information_publicity_regulation_2024_moj.html` | 2024 版国家行政法规库 | 可作为现有来源证据, 还应下载其官方 Word/PDF 以降低网页 DOM 改版风险。 |
| `data/knowledge/sources/certification_accreditation_regulation_2023_moj.html` | 认证认可条例国家行政法规库 | 可作为现有来源证据, 但版本开始日期需要同 2023 修订事实对齐。 |
| `data/knowledge/sources/compulsory_product_certification_rules_2025_isccc.html` | 强制性产品认证管理规定转载页 | 可用于语料准备, 但应补制定机关原件后再作为生产版本权威源。 |
| `data/knowledge/sources/product_quality_law_cnipa.html` | 产品质量法国家知识产权局转载页 | 可作为官方部门转载快照, 建议补全国人大一手法源。 |
| `data/knowledge/raw/enterprise_information_publicity_regulation_v2024.md` 与 `certification_and_accreditation_regulation_v2023.md` | 上述两部法规的清洗文本 | 属于索引输入, 不是原始来源快照; 它们必须通过版本键、节点定位符和 `body_sha256` 回指到 `sources/` 下的原件。 |

## 入库顺序和边界

1. 先补齐缺失的三份来源: 2014 原版《企业信息公示暂行条例》、市场主体登记管理条例、实施细则。每份先落 `sources/` 原始字节, 再更新 `source_catalog.json`。
2. 再为两组存在历史修订的法规建立独立版本: 企业信息公示条例 2014/2024, 认证认可条例至少 2020/2023。只有先后版本均为本地快照, `supersedes` 和 `as_of` 回放才有可验证意义。
3. 清洗为 `raw/` 文本时保留条、款、项和表格行定位。`RetrievalChunk` 只能是检索投影; 最终引用必须指向一个带 `edition_key`、定位符、字符区间和正文哈希的节点。
4. 强制认证和产品质量法规只在案件事实已证明目录适用、具体认证类型或产品范围时作为条件性背景。它们不得生成“某品类必须提交质量证书”的通用结论。

## 资料适配性

这些资料与 VendorGuard 的“供应商材料形式齐备性和字段一致性”范围相符, 因为它们提供营业执照字段、公示、认证目录和质量认证的外部语境。它们不适合承担准入结论本身: 法规的规范对象多为登记机关、认证机构、生产者或销售者, 与本公司供应商准入制度不同。

因此一期应保留现有语料候选中的角色划分: 市场主体登记条例及实施细则为 `background`; 企业信息公示条例为 `background` 和 `version_negative`; 认证认可条例、强制性产品认证管理规定为 `conditional_reference`; 产品质量法为 `version_negative` 或反向适用性测试。任何 `direct_policy` 命中必须来自版本化内部制度。
