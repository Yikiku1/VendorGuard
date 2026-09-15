# M3：把制度依据接进同一个 Agent 循环

> 日期：2026-09-14。
> 修订：2026-09-15，主链限定为现行制度与节点引用；全量索引和 BM25 改为可选实验。
> 状态：方案已定义，实现未开始。
> 当前唯一小功能：M3-1 片段清单落盘。
> 上游依据：[PRD](VendorGuard-PRD.md) 的 RAG 核心范围、[Agent 开发计划](VendorGuard-Agent开发计划.md) 与
> [M2 实施方案](VendorGuard-M2实施方案.md)。

## 1. M3 要交付什么

M2 让 Agent 能读真实材料并核对来源，但结论只基于材料本身。M3 给它接上**制度依据**：
检索现行内部制度、引用到具体条款、依据不足时明确说明，最后交付一份带来源的结构化报告。

```text
用户提交材料 + 一句请求
    -> Agent 调用 read_material 读材料
    -> Agent 调用 check_materials 提交事实与来源（M2 已有）
    -> 需要制度依据时调用 search_policy(query)
    -> 程序只返回现行制度的片段, 并记录"本次真实返回过哪些可引用节点"
    -> Agent 调用 submit_report 交付带引用的报告
    -> 程序核对: 规则结论来自本次校验, 节点引用来自本次检索, 否则拒绝并纠正
    -> 信息缺失时仍走 ask_user, 用户补充后在同一会话重新校验和检索
```

M3 完成后仍然只有一个 Agent 和一个工具循环，`agent.py` 是唯一运行入口；CLI 仍是唯一入口，
不新增第二个运行器。M2 的预算、纠正、工具事件与脱敏记录机制由 M3 复用。

## 2. 范围

### 本阶段实现

- 从两份现行制度生成**一份扁平片段清单**并落盘入库，字段含片段与节点 ID、正文、标题、制度版本、
  定位路径、归一化原文路径和快照哈希；运行时仍用固定版本清单限定范围。
- 复用 `parse_policy_edition()`，不重写分块算法；旧版制度和外部法规只用已有元数据测试版本过滤。
- 本地 embedding 缓存（模型名 + 文本哈希失效即整体重建），精确 cosine 计算相似度。
- 单路向量检索，固定返回最多 5 个候选；**候选集先按版本清单过滤到现行两份制度**。
- 新增业务工具 `search_policy(query)`；新增交付工具 `submit_report(...)`。
- 引用闸门：制度引用节点必须来自本次运行真实返回，材料与规则结论必须对应本次成功的校验结果。
- 依据不足时明确说明，不硬答；8 题固定评测 + 一次真实失败分析。

### 本阶段不实现

- 数据库入库、pgvector 列、`pg_search`/Tantivy、RRF、reranker、ANN 索引。
- 扩充法规库、任意历史日期回放、版本血缘查询、增量入库服务。
- 独立 EvidenceEngine、证据槽位持久化、审批状态联动、前端工作台与报告确认界面。
- 扫描件 OCR、复杂版面解析（MinerU 一类工具在本阶段没有使用场景，见 §4 说明）。
- 全量 274 条片段的索引与 embedding、纯 Python BM25 对比；闭环后的漏检实验按 §8 单独决定。

## 3. 三个必须守住的边界

### 3.1 模型不能凭空引用

`RetrievalChunk` 只用于召回，最终制度引用回指 `KnowledgeNode`。`search_policy` 同时返回
`chunk_key`、`node_key`、版本、定位与原文；循环只登记本次真实返回的 `node_key` 和对应来源。
`submit_report` 里的节点引用必须在这份本次运行的集合中，并与版本、定位、原文区间对应，
否则拒绝并给一次纠正。模型不能引用没检索到的节点或被过滤的版本。材料来源另由本次
`check_materials` 的已核对事实限定，不把"页码存在"误当成对任意结论的支持。

### 3.2 候选集由程序限定，不由模型或提示词决定

版本过滤发生在程序侧：以明确的现行清单（`demo_supplier_admission_policy_v2`、
`demo_supplier_required_documents_policy_v3`）限定候选，`as_of` 由程序给定。
旧版本与外部法规**保留在仓库**作为负例；版本过滤测试向候选输入混入这些版本的元数据，
证明程序在排序前排除它们，而不要求为负例生成 embedding。仅在现行语料中搜不到旧版
不能证明过滤。模型不能要求扩大范围或回放历史版本。

### 3.3 依据不足必须明说，分数不能当证据

相似度最高的片段也可能不支持结论，分数跨请求不可比（供应商明确说明 `relevance_score`
不代表可比性），因此判定一律基于**排名与引用核对**，不设分数阈值。Top-5 无法支持结论时
必须说明依据不足；"自然语言结论是否真被引用支持"由固定案例人工核对，程序不假装解决。

## 4. 为什么本阶段不用数据库、不用 MinerU

这两条都是有意为之，不是省事：

- **数据库**：现行候选只有 22 条，精确 cosine 全量计算不需要 ANN 索引；语料的权威副本
  是冻结快照与归一化正文（哈希可复核）。片段是由代码确定性算出的派生物，入库只会多出
  一份可能漂移的状态。检索真正吃紧时再启用
  `evidence/models.py` 里已暂停的五表与迁移（属于"启用"，不是"重写"）。
- **MinerU 一类版面/OCR 工具**：它们解决"复杂版面或扫描件 PDF → 结构化文本"。本阶段的
  对象是内部制度，仓库里已有归一化正文与解析器；材料侧 M2 已明确扫描件在材料层拒绝、
  不做 OCR。引入它等于同时推翻这两条边界，还没有对应收益。等到要接收真实供应商提交的
  任意 PDF 时（M4 之后），先改"不支持扫描件"这条边界，再谈解析器选型。

## 5. 模块与接口

本阶段新增一个检索包（三个内部模块）与一个报告模块；Agent 只调用检索和报告的接口。

| 文件 | 作用 | 对外接口 |
| --- | --- | --- |
| `src/vendorguard/retrieval/chunk_list.py` | 从两份制度生成、落盘、加载扁平片段清单 | `build_chunk_records(knowledge_dir) -> tuple[ChunkRecord, ...]`；`write_chunk_list(path, records)`；`load_chunk_list(path) -> tuple[ChunkRecord, ...]` |
| `src/vendorguard/retrieval/embedding.py` | embedding 客户端、正文缓存与查询向量 | `load_or_build_vectors(chunks, *, client, model, cache_path)`；`embed_query(query, *, client, model) -> tuple[float, ...]` |
| `src/vendorguard/retrieval/search.py` | `search_policy` 查询接口，内部完成查询向量、现行版本过滤与精确 cosine 排序 | `search_policy(query, *, records, vectors, embedding_client, model, top_k=5) -> tuple[RetrievedChunk, ...]` |
| `src/vendorguard/agent_report.py` | 报告数据模型、实际事实/规则结果与引用核对、越权措辞拦截 | `parse_report_arguments(raw_json) -> ReviewReport`；`verify_report(report, *, checked_facts, check_result, returned_nodes) -> None` |

修改现有文件：

| 文件 | 修改内容 |
| --- | --- |
| `src/vendorguard/agent.py` | 现有循环内加入 `search_policy` 分发、本次返回节点登记与 `submit_report` 出口；`AgentRunOutcome` 保存通过校验的报告，纯文本不再作为检查结论出口 |
| `src/vendorguard/config.py`、`.env.example` | 新增 `VENDORGUARD_EMBEDDING_MODEL`（默认 `qwen3.7-text-embedding`）与缓存路径 |
| `pyproject.toml`、`uv.lock` | 新增 `numpy`（精确 cosine 与缓存读写；不引入向量数据库） |
| `README.md`、`PROJECT_CONTEXT.md`、开发计划 | 只在 M3 验收后更新为"已实现并验证" |

`retrieval` 包隐藏 embedding 协议、缓存格式与打分细节。Agent 只需要用已加载的数据调用
`search_policy(query)` 并接收片段与节点的对应关系；测试从同一接口验证查询、过滤与排序，
不依赖 `numpy` 数组或缓存格式。

## 6. 数据与片段契约

现行两份制度的片段清单落盘为 `data/retrieval/chunks_v1.jsonl`（一行一条，入库提交）。每条记录：
构建时从冻结清单读取选中版本的元数据与归一化正文，核对快照和正文哈希后调用
`parse_policy_edition()`；不调用 `parse_frozen_corpus()` 作为 M3-1 的必经入口。

| 字段 | 说明 |
| --- | --- |
| `chunk_key` | 稳定 ID，沿用解析器产出的键（如 `demo_supplier_admission_policy_v2_section_2_main`） |
| `node_key` | 解析器对应的可引用节点 ID；检索片段本身不作为最终引用单位 |
| `edition_key` | 制度/法规版本键 |
| `document_title` | 文档标题，只作展示与检索上下文前缀 |
| `locator_path` | 版本内定位路径（章节标题、表格行号等），用于回指原文与评测比对 |
| `display_text` / `search_text` | 展示原文 / 送入检索的文本（含上下文前缀），沿用 `RetrievalChunk` 的既有区分 |
| `char_start` / `char_end` | 归一化正文中的字符区间，用于证明能回指原文 |
| `source_path` | 归一化正文的仓库内相对路径 |
| `snapshot_sha256` / `normalized_sha256` | 冻结快照与归一化正文哈希，复用解析器给出的来源指纹 |

实测基线（2026-09-14，用现有解析器跑全量语料）：

| 语料 | 正文字数 | 片段数 |
| --- | --- | --- |
| `demo_supplier_admission_policy_v2` | 881 | 6 |
| `demo_supplier_required_documents_policy_v3` | 1374 | 16 |
| **两份现行制度合计（过滤后的候选集）** | 2255 | **22** |
| 其余 8 篇（负例：旧版本 + 外部法规） | 30989 | 252 |
| **全量（已有解析器基线，非本期索引池）** | 33244 | **274** |

现有解析器全量基线为 274 条，其中本期现行制度为 22 条；M3-1 只提交这 22 条。
负例 252 条保留在仓库供过滤测试，不进入本期向量缓存。按 batch 上限 20，正文缓存约需
2 批（实际调用数以探针和运行记录为准）。Top-5 仍须按相关性排序，不能因候选少就省略检索。

## 7. 工具契约

### `search_policy`

输入：

```json
{"query": "关键物料品类是否可以先准入后补交质量证书", "top_k": 5}
```

输出：

```json
{
  "as_of": "2026-09-01",
  "scope": ["demo_supplier_admission_policy_v2", "demo_supplier_required_documents_policy_v3"],
  "results": [
    {
      "chunk_key": "demo_supplier_required_documents_policy_v3_section_4_table_row_2_main",
      "node_key": "demo_supplier_required_documents_policy_v3_section_4_table_row_2",
      "edition_key": "demo_supplier_required_documents_policy_v3",
      "title": "关键物料必需材料",
      "locator": ["关键物料必需材料", "第2行"],
      "text": "| critical_material | 质量证书 | 必需 | 剩余有效期不足 90 天须复核 |"
    }
  ]
}
```

运行路径：先校验查询，再为查询生成向量并核对维度；程序按固定版本清单过滤候选，
对候选的缓存正文向量计算 cosine，按分数排序（同分按 `chunk_key`），返回最多 5 条。
每条 `node_key` 必须由本次片段对应的节点提供，不能由模型自行传入或由 `chunk_key` 猜出。

约束：`top_k` 缺省 5、上限 5；`results` 只含现行制度片段；查询为空、过长或非字符串为
参数错误，可走现有纠正通道。启动时清单或缓存缺失/损坏、查询 embedding 失败或维度不符
属于检索失败，不能伪装成"没有依据"或让模型用参数纠正机会反复重试。模型不能通过该工具
枚举目录、读取任意文件或检索负例版本。

### `submit_report`

输入（最小字段集，实施时以钉住的 Schema 为准）：

```json
{
  "material_id": "license_complete",
  "findings": [
    {
      "summary": "营业执照声明有效期至 2027-08-31，按参考日期判定为有效；VEN-001 未命中",
      "fact_fields": ["business_license_valid_until"],
      "rule_results": [{"rule_id": "VEN-001", "result": "not_hit"}],
      "material_sources": ["license_complete@page:1"],
      "policy_citations": []
    },
    {
      "summary": "正常准入条件要求营业执照未过期且关键字段可辨识",
      "fact_fields": [],
      "rule_results": [],
      "material_sources": [],
      "policy_citations": ["demo_supplier_admission_policy_v2_section_2"]
    }
  ],
  "insufficient_evidence": false,
  "missing_reason": "",
  "notes": "规则未命中不等于准入批准"
}
```

处理顺序固定：

1. Pydantic 校验字段、类型与多余参数；提交报告前必须有本次成功的 `check_materials` 结果，
   有 `missing_fields` 时仍通过 `ask_user` 追问，不把未核实的事实写成报告。
2. 每条 finding 至少一个来源。`fact_fields` 必须属于本次成功提交的事实，其
   `material_sources` 必须与 `verified_sources` 中对应的定位一致；`rule_results`
   的规则 ID 和 `result` 必须与本次 `checked_rule_ids` / `evaluations` 对应。
   程序校验这些结构字段与真实结果，不宣称能核对任意自由文本摘要。
3. 制度性判断必须引用本次检索返回的 `node_key`；程序核对节点的版本、定位、字符区间
   与原文，不能把召回用 `chunk_key` 当成最终引用。自然语言是否受原文支持另由固定案例人工核对。
4. 措辞检查：不得宣告"准入批准/审批通过"，不得声称系统已写入状态。
5. 任一步失败都不产出报告，错误作为工具结果回给模型纠正。

`insufficient_evidence=true` 时允许 `findings=[]` 且零引用，必须在 `missing_reason` 说明
无法得出的结论及缺少哪类依据；此时不得同时声称审查完整。依据缺失不等于材料事实缺失：
前者可输出明确的不足报告，后者继续走 `ask_user`。上面的样例分别展示已核对的材料/
规则结果和制度要求；第二条的节点引用必须先由本次 `search_policy` 实际返回。

### 沿用 `read_material` / `check_materials` / `ask_user`

M2 的三个工具保持不变：材料白名单、来源核对、追问出口与预算机制都不重新设计。

## 8. 检索方案与可选对比

- **首版向量路**：精确 cosine（numpy），候选先按现行版本清单过滤，再按相似度与
  `chunk_key` 排序取 Top-5；同一查询与固定向量的排序必须可复现。
- **闭环后实验**：8 题中若有具体漏检，针对该题用纯 Python BM25（中文字符二元组）
  对比关键词与向量 Top-5，记录必需依据的位置与失败原因。实验不阻塞 M3 验收，
  不引入 ParadeDB、Tantivy 或 RRF；是否扩展全量索引池由实验结果决定。

## 9. 实施步骤

每步只提交一个可运行小功能。后一步不得提前混入。

### M3-0：冻结基线

目的：证明后续失败由 M3 改动引入。

```powershell
uv run --no-sync pytest -q -p no:cacheprovider tests/unit
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy
```

基线：2026-09-14 已验证 `247 passed`，Ruff、格式与 mypy 通过（M2 收尾状态）。

### M3-1：片段清单落盘

修改范围：`retrieval/chunk_list.py`、对应测试、`data/retrieval/chunks_v1.jsonl`。

先写聚焦测试：两份现行制度的清单与解析器一致（22）；每条 `chunk_key` 对应一个
`node_key`，`char_start/char_end` 能在归一化正文里回指 `display_text`；清单加载后
与构建结果逐字段一致。负例制度与外部法规不写入清单。

完成判据：能从命令行生成清单并打印统计（现行 22），清单入库；选择器不会把旧版或
外部法规写入清单。排序前混入负例并验证过滤留给 M3-3。274 / 252 是解析器全量基线，
不是本步入库条数。

### M3-2：embedding 客户端与本地缓存

修改范围：`retrieval/embedding.py`、对应测试、`config.py`、`.env.example`、依赖。

先做的事：跑一次真实 embedding 探针（一次调用即可），记录模型名是否可用、维度、是否
已 L2 归一、响应是否按 `index` 回映射；结论写进测试与文档。文档记录的预期是
`qwen3.7-text-embedding`、1024 维、已归一、batch 上限 20。

先写聚焦测试（全部用替身，不打网络）：正文分批不超过 20；响应乱序时按 `index` 回映射；
正文和查询向量的维度、有限值与 L2 范数按探针结果校验；缓存按"模型名 + 文本哈希"
判断失效，模型或正文变化时整体重建。查询向量每次请求生成，不混入正文缓存。

完成判据：22 条正文的缓存生成且二次运行不重复嵌入正文；给定查询可生成同维向量。

### M3-3：向量检索与版本过滤

修改范围：`retrieval/search.py`、对应测试。

先写测试：程序按现行版本清单过滤，即使混入更高分的负例候选也不返回；Top-5 数量
与同分排序在固定向量下确定；无候选、维度不符与 `top_k` 超限均有明确行为。

完成判据：给定真实查询能返回确定的 Top-5、节点 ID、原文与定位，命令行可复现同一排序。

### M3-4：`search_policy` 接进循环

修改范围：`agent.py`、`test_agent.py`。

钉住调用顺序与错误分流：未读材料不能 check（M2 已有）；成功检索只登记本次真实
返回的 `node_key` 与原文对应关系；参数非法走现有纠正通道，检索配置/embedding 失败
直接失败且不产出报告；一次模型响应仍只允许一个工具调用，预算沿用 M2 限制。

完成判据：测试替身走通 `read -> check -> search`，仓库仍只有一个循环一个 CLI。

### M3-5：带来源的结构化报告

修改范围：`agent_report.py`、`agent.py`、对应测试。

先写失败用例：未成功 check 就提交报告、捏造 `fact_fields` 或 `rule_results`、材料事实来源
不是对应的 `verified_sources`、引用未返回的 `node_key`、引用负例版本、宣告准入批准、
零引用却声称完整——全部拒绝且不产出报告；明确缺依据的空 findings 报告可通过。

完成判据：测试替身走通 `read -> check -> search -> report`；只有成功的 `submit_report`
能产生 `kind=answer` 与结构化报告，校验失败不产出报告；CLI 打印报告与引用清单。

### M3-6：一次补充后重新检索

修改范围：`agent.py`、`test_agent.py`、必要的 CLI 测试。

一次补充仍由 `ReviewSession` 保留用户原文，但 CLI 第二轮重新调用 `run_review()`，
必须重新读取、校验和检索。本次节点登记仅在该次运行内有效；第二轮直接引用第一轮
返回的节点应被拒绝，不能把旧检索结果自动沿用为新事实的依据。

完成判据：补充案例的第二轮能提交仅使用第二轮实际来源的报告；无需长期会话管理。

### M3-7：8 题评测、真实运行与文档收尾

修改范围：评测脚本、`data/evals/`（只增不改原数据）、运行记录、三份文档。

评测只选 PRD 指定的 8 题（用题目 ID 引用原记录，不改写原数据）：3 道正常题看必需依据是否
进入 Top-5，5 道边界题看是否按各自金标准处理，最后统计错误或无效引用数。边界题中
有需引用现行制度纠正前提的案例，不把五题一概按拒答处理。**不能把这 8 题结果说成
完整 15 题成绩。**若有具体漏检，按 §8 单独做关键词对比并记录，不阻塞主链验收。

真实运行：完整材料样例、缺日期样例（一次补充闭环）、扫描件样例各跑一次，真实模型只验证
工具选择与端到端交互，确定性边界由单元测试负责。运行记录必须包含模型、材料哈希、工具参数
与结果、来源核对、本次实际返回的节点、用户补充轮次、耗时与 token 用量，不含 API Key 与整份原文。

文档收尾：开发计划的 M3 状态、README 与 PROJECT_CONTEXT 同步；真实模型有波动时记录实际
失败，不挑选一次成功就宣称稳定。

## 10. 验收清单

M3 只有同时满足以下条件才算完成：

- 仓库只有 `agent.py` 一条 Agent 循环和一个 CLI 入口。
- 两份现行制度的 22 条片段可复核：每条的节点 ID 与原文区间能回指归一化正文。
- 候选集只含现行两份制度；混入负例候选元数据也不出现在结果里，证明排序前过滤有效。
- 同一查询的 Top-5 可复现；分数不参与阈值判定。
- 制度引用节点必须来自本次真实返回；伪造引用、引用负例版本、未核对的材料来源或
  与实际规则结果不符的报告一律拒绝。第二轮补充后必须重新检索。
- 依据不足时说明缺什么依据，不硬答；报告不得宣告准入批准。
- 8 题逐题结果与 PRD 的三项汇总，不冒充完整 15 题成绩；漏检后的 BM25 对比是可选实验。
- 至少一次完整案例、一次补充案例、一次扫描件案例的真实运行记录。
- 相关单测、全量单测、Ruff、格式检查与 mypy 通过。
- 不访问数据库、不修改案件状态、不接前端；`evidence/models.py` 的五表保持暂停不动。

## 11. 开始方式

下一次开发只做 M3-1。第一条命令用于确认基线：

```powershell
Set-Location D:\projects\VendorGuard
uv run --no-sync pytest -q -p no:cacheprovider tests/unit
```

随后先创建 `tests/unit/test_retrieval_chunk_list.py` 的第一个失败测试：只对两份现行制度
构建片段清单后断言条数为 22，且每条 `node_key` 能回指解析器节点。看到测试因
`vendorguard.retrieval.chunk_list` 不存在而失败后，再建模块，只实现使这一个测试通过的最小代码。

M3 不使用向量数据库、不使用 ParadeDB、不引入 LangGraph。全量索引与 BM25 对比只有
在 M3-7 定位具体漏检后才作为单独实验考虑；混合检索和数据库不自动转成必做项。
