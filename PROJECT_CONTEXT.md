# VendorGuard 当前上下文

> 更新：2026-09-15。M3 已完成（制度检索与结构化报告）；项目定位为 AI Agent / AI 应用开发求职。

## 当前定位与入口

VendorGuard 是供应商材料审查 Agent：用户提交材料，Agent 调用读取、校验和制度检索工具，缺信息时追问，最终输出带来源的初审报告，供用户确认。

- 产品范围与技术取舍：[PRD](project_docs/VendorGuard-PRD.md)。
- 当前工作与完成标准：[Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md)。
- 开发协作规则：[AGENTS.md](AGENTS.md)。
- 修改已有案件状态、规则或数据库时，按需查阅状态数据字典、规则 YAML 和现有迁移；不再要求每次开发完整阅读领域调研。

旧十天计划已停止执行。旧 Day 4 / Day 6 完成条件不再阻塞 Agent 开发，未完成事项也不因此变成已完成。

## 当前代码事实

以下来自本轮文件核对，不代表本轮重新通过运行验证。

| 能力 | 当前证据与边界 |
| --- | --- |
| 后端与认证 | FastAPI、数据库会话、JWT、配置和日志实现已存在 |
| 供应商与案件 | 创建、材料元数据登记、查询、规则评估及人工决定接口已存在；并非完整页面闭环 |
| 确定性规则 | `policy/schema.py`、`policy/evaluator.py`、`policy/facts.py` 已存在；启用规则仍为 `VEN-001`、`VEN-002` |
| 知识资料与评测 | 已有冻结语料、归一化正文和 `data/evals/rag_phase1.json` |
| 结构节点解析 | `evidence/node_parser.py` 与对应测试已存在；旧交接中“尚未实现”的描述已过时 |
| RAG 持久化 | `evidence/models.py`、迁移 `b0604f36e9a4` 已存在，包含四层实体和版本关联表；本轮开始时两者尚未提交，`alembic/env.py` 也有已有改动 |
| Agent M1 | `agent.py` 与 `agent_tools.py` 已实现真实模型工具循环；预算、错误纠正和脱敏记录机制仍在使用，M1 的快照事实入口（`parse_tool_arguments` / `check_materials`）已删除 |
| Agent M2 材料输入 | `materials.py` 逐页读取文本 PDF（失败码 `file_not_found`、`not_a_pdf`、`scanned_pdf_unsupported`、`empty_material`）并提供来源核对 `MaterialSources`；`agent_tools.py` 的提取事实管线在规则执行前核对来源、由程序派生执照状态；`agent.py` 的循环先 `read_material` 再 `check_materials`；`agent_session.py` 持有材料与用户补充，支持一次补充闭环。三份自制样例与生成脚本在 `data/demo/materials/`、`scripts/make_demo_materials.py` |
| Agent M3 制度检索 | `retrieval/chunk_list.py` 从冻结语料生成现行两份制度的 22 条片段并落盘 `data/retrieval/chunks_v1.jsonl`，每条可用 `node_key`、定位路径、字符区间与两份哈希回指解析器节点和归一化正文；候选范围由代码白名单 `CURRENT_EDITION_KEYS` 限定，旧版与外部法规留在仓库作负例，构建入口 `scripts/build_chunk_list.py`。`retrieval/embedding.py` 按 20 条分批生成正文向量并缓存到 `data/retrieval/cache/`（gitignore 不入库），缓存按"模型名 + 正文指纹"整体失效，查询向量不入缓存，构建入口 `scripts/build_embedding_cache.py`。`retrieval/search.py` 先按白名单过滤再算精确 cosine（Top-5，同分按 `chunk_key`；参数错误抛 `QueryError`、配置失败抛 `SearchError`）。`agent.py` 的循环已接入 `search_policy`（启动装载 `load_policy_index`、登记本次真实返回节点、工具结果不含分数）与 `submit_report`（唯一结论出口：纯文本不再产生 `kind=answer`），报告经 `agent_report.py` 的引用闸门核对后才写进 `AgentRunOutcome.report` 与运行记录；节点登记与校验结果都是**单次运行内的局部状态**，CLI 补充轮重新调用 `run_review()` 并逐轮打印报告。检索数据与命令：`data/retrieval/chunks_v1.jsonl`（22 条）、`data/retrieval/cache/`（向量，gitignore），脚本 `scripts/build_chunk_list.py`、`build_embedding_cache.py`、`preview_search.py`、`run_rag_eval.py`（8 题评测结果在 `data/evals/rag_phase1_m3_8q_result.json`）。前端与工作台（M4）尚未实现 |
| 后续产品链路 | 制度检索、带引用报告和前端尚未实现；跨轮对话只支持一次补充，未做长期会话管理 |

当前案件评估中的正常路径仍停在 `analyzing`，补件路径可到 `pending_documents`；已有人工决定接口不等于端到端审批已跑通。Agent 初审报告不依赖推进这些业务状态，也不能把报告确认写成供应商准入批准。

## 已有成果如何处理

已完成占位清理：删除两个空模块、两个旧策略目录占位和五个多余 `.gitkeep`，移除 AGENTS.md 的旧 Pi 安装提示。具体路径见 M1 实施方案的清理记录。实际业务逻辑、测试、依赖、配置和数据库未改变，也没有撤销已有迁移或用户未提交改动。当前数据库实际迁移版本和服务健康状态未重查，不能使用历史成功记录代替现场验证。

后续优先复用规则纯函数、事实 Schema、制度正文及节点定位。RAG 持久化和案件审批保留在原处，暂停扩展。将来决定删除时，需要先确认代码引用与实际数据库状态，再单独处理。

2026-09-08 后续范围确认：RAG 仍是核心能力，按 [PRD 的 RAG 核心范围](project_docs/VendorGuard-PRD.md#rag-核心范围) 精简为两份现行制度、扁平片段、本地向量检索、引用校验与 8 题评测。复用内部制度解析器；全法规解析、多表入库、版本血缘和多路召回暂停。既有 RAG 语料、解析器和持久化资产保留，但 M2 不扩展检索能力。

旧文档记录的测试结果属于历史验证。占位清理按删除路径、代码引用和导入检查验证，不将其视为业务功能或数据库重新验收。

## 进度记录与当前下一步

2026-09-10：M1 已完成（经两轮外部评审修正闭环）。命令行 Agent（`python -m vendorguard.agent <事实样例> [请求]`）在 qwen3.7-flash 自动工具选择下跑通正常/缺件/未知/超范围四场景：前三者通过（未知案例为“校验后经 ask_user 切题追问”），超范围交付率场景三次运行均未达成切题追问（全为安全失败、伪造被拦截），记为已知限制；详见 [M1 实施方案](project_docs/VendorGuard-M1实施方案.md)与[开发计划](project_docs/VendorGuard-Agent开发计划.md)的评审修正记录。

2026-09-10 M2 衔接记录：[M2 详细方案](project_docs/VendorGuard-M2实施方案.md)当时只规划真实材料读取、来源核对和一次补充闭环，不接 RAG、报告、前端或 LangGraph。原生 DashScope SDK 入口在本账号报 url error，Agent 走 OpenAI 兼容协议与 `openai` SDK（实测记录在 M1 方案第 5 节）；追问使用显式工具 `ask_user`；工具参数与来源核对是规则执行前的安全边界，结论必须有工具背书。

2026-09-13：M2 进入实现。M2-0 基线复核通过（当时 211 项单测与 Ruff/格式/mypy 全绿，工作区无混入源码改动）；M2-1 文本 PDF 读取、M2-2 来源核对与提取事实管线完成。为保持每一步可运行，M1 的快照入口与 M2 的提取事实管线一度并存，M2-3 把循环切过去后已删除前者。

2026-09-14：M2 完成（M2-0 至 M2-5）。`agent.py` 保留唯一循环与唯一 CLI，CLI 现在接收材料 PDF 并可传 `--reference-date`；`agent_session.py` 支持一次补充闭环；全量单测 247 通过，Ruff、格式检查与 mypy 全绿（Docker 未启动，集成测试未跑，本轮改动不涉及数据库）。真实模型记录在 `logs/agent-runs/`：完整样例 3 次全过，扫描件在材料层被拒且未发模型请求，缺日期样例 3 次里 1 次走完闭环、2 次为模型波动（耗尽纠正预算后失败、以及补充后仍选择追问），两次都按“模型行为方差、边界未放行”记入开发计划的已知限制。

2026-09-15：M3 方案修订并提交（`8fa46cc`），M3-0 基线复核通过，M3-1、M3-2 完成。当前唯一小功能是 M3-3：向量检索与版本过滤；详细接口、步骤与验收清单见 [M3 实施方案](project_docs/VendorGuard-M3实施方案.md)。全量 274 条仅作为已有解析器的实测基线；旧版与外部法规元数据用于过滤负例测试，不生成本期负例向量。查询向量与正文缓存同维校验，制度引用回指本次检索实际返回的 `KnowledgeNode`；报告的结构化事实字段和规则结果对应本次成功的 `check_materials`，自由文本是否受来源支持仍由案例人工核对。补充后重新检索。BM25 与全量索引只在闭环后针对具体漏检单独考虑；M3 仍不访问数据库、不用 MinerU 一类版面解析、不新增领域实体。

M3-0 复核为 247 项单测与 Ruff、格式检查、mypy 全绿，提交三份文档后工作区无其它改动。M3-1 完成后新增 `retrieval/chunk_list.py` 与 `scripts/build_chunk_list.py`，清单落盘 22 行 / 27037 字节（6 + 16，sha256 `45b39ff0…`）；新增 11 项单测，全量单测 258 通过，Ruff、格式检查与 mypy 全绿（本步不涉及数据库，集成测试未跑）。

2026-09-15 M3-2 探针与验证：探针 `scripts/probe_embedding.py` 一次真实调用确认 `qwen3.7-text-embedding` 可用、1024 维、返回向量已 L2 归一（范数 1.000000）、响应带 `index` 且与输入同序；单次输入上限 20 条（21 条被服务端 400 拒绝）。由此新增 `retrieval/embedding.py`（正文分批 ≤20、按 `index` 回映射、维度/有限值/单位范数校验、缓存按"模型名 + 正文指纹"整体失效、查询向量不进缓存）、`config.py` 的 `embedding_model` 与 `embedding_cache_path`、`.env.example` 与 `.gitignore` 对应条目，并加入 `numpy` 依赖；聚焦测试 18 项全用替身、不打网络。临时落盘实测：22 条正文 2 次请求建成缓存（约 499 KB），二次运行 0 次新请求且向量逐分量一致，查询向量 1024 维；全量单测 278 通过，Ruff、格式检查与 mypy 全绿。开发者粘贴后已在仓库跑绿：`scripts/build_embedding_cache.py` 首次 2 次调用（20 + 2 分批）建成 `data/retrieval/cache/embedding_v1.json`（499719 字节，gitignore 不入库），二次 0 次调用复用；全量单测 278 通过。

补一条影响后续口径的实测：同一批正文两次独立构建的向量并非逐分量相同（22528 个分量中 7168 个有差异，最大 2.0e-4），所以"同一查询的 Top-5 可复现"只能绑定固定的缓存文件，不能宣称模型输出稳定；缓存同时承担"冻结向量"作用。

2026-09-15 M3-3 测试与验证：`retrieval/search.py` 先按现行白名单过滤候选再算精确 cosine，Top-K 缺省与上限均为 5、同分按 `chunk_key`；查询参数问题抛 `QueryError`（留给模型纠正），配置类失败（候选被过滤光、向量与片段数量不符、维度不符）抛 `SearchError`（直接中断，不冒充"没有依据"）。18 项单测全部用替身，负例向量刻意造成与查询同向，证明过滤发生在打分之前。命令行验证 `scripts/preview_search.py`（真实缓存）返回确定的 Top-5（含 `node_key`、定位、区间、原文），重复检索排序一致；**两条检索的分数在第 4 位小数会漂移**（查询向量每次现算），排序稳定依赖分差远大于漂移，再次说明分数不能当阈值。全量单测 296 通过，Ruff、格式检查与 mypy 全绿。

2026-09-15 M3-4 测试与验证：`agent.py` 循环接入 `search_policy`：新增启动装载 `load_policy_index()`（清单缺失、缓存损坏或 embedding 不可用归一为启动错误，退出码 2）；成功检索登记本次真实返回的 `node_key` 与原文（`returned_nodes`，M3-5 的引用闸门只认它）；给模型的工具结果含 `as_of`/`scope`/节点定位与原文但**不含分数**；参数问题走共享纠正通道，检索配置或 embedding 失败直接失败且不再要结论；预算与"一次响应一个工具调用"沿用 M2。检索不要求先读材料或先校验（只读查询，方案只钉住"未读材料不能 check"）。新增 9 项替身用例，全量单测 305 通过，Ruff、格式检查与 mypy 全绿；CLI 启动路径用"模型端点指向死端口"实测：先打印 `[制度检索] 已装载 22 条现行制度片段` 再进入循环，退出码 1，未消耗真实模型调用。

2026-09-15 M3-5 完成：新增 `agent_report.py`（`ReviewReport`/`Finding`/`RuleResultClaim`、`parse_report_arguments`、引用闸门 `verify_report`、越权措辞黑名单）。闸门核对结构字段：材料身份、`fact_fields` 属本次已核对事实、`material_sources` 与事实对应、`rule_results` 与本次 `evaluations` 逐字一致、`policy_citations` 必须是本次检索返回的现行节点；Schema 层挡住"零 findings 却声称完整"，依据不足必须有 `missing_reason`。`agent.py` 的结论出口改为**只有成功的 `submit_report`**，纯文本不再收尾；报告写进 `AgentRunOutcome.report` 与运行记录，CLI 逐条打印结论、事实、材料来源与制度引用。新增 23 项报告单测 + 5 项循环用例，并改写 M1/M2 时代以文本收尾的 14 处用例；全量单测 335 通过，Ruff、格式检查与 mypy 全绿。

2026-09-15 真实运行与一处修正：完整样例第一次跑到 `submit_report` 被**措辞检查误杀**（模型写的是免责声明"…但不等同于最终准入批准"，黑名单只认"不等于"），补进"不等同/不表示/不意味着/不能说明"并把该真实句子固化为测试后，重跑通过（read → check → search → report，退出码 0，打印 2 条发现与 3 个现行节点引用）。措辞检查是黑名单兜底，可能误伤免责表述也可能漏掉改头换面的宣告；真正保证是结构字段与固定案例人工核对。

2026-09-15 M3-6 完成：第二轮被证明是独立的一次运行（`returned_nodes`、`checked_result` 都是 `run_review()` 的局部状态；CLI 补充轮重新调用它，开场只带材料与补充原文）。新增 4 项循环用例 + 3 项 CLI 用例，并修掉一个真实缺陷：CLI 的报告打印块原本只覆盖第一轮，补充轮只打印一行摘要——现抽成 `_print_report()` 由每轮调用；另在校验缺字段后的 user 提醒里点名"必须调用 ask_user 追问，不能提交报告"（实测模型缺字段时会反复去交报告）。全量单测 343 通过，Ruff、格式检查与 mypy 全绿。

2026-09-15 M3-7 与 M3 收尾：评测脚本 `scripts/run_rag_eval.py` 只读原评测数据里 PRD 选定的 8 个 `case_id`，结果写入 `data/evals/rag_phase1_m3_8q_result.json`（原数据未改，其余 7 题明确未覆盖、不算进成绩）。逐题结果：3 道正常题必需依据全部进 Top-5（3/3）；2 道需纠正前提的边界题也全部进 Top-5（2/2）；3 道 `no_answer` 题标为待人工核对（检索不设阈值、恒返回 Top-5，是否判依据不足不由检索决定），并逐题列出 Top-5 与"现行版本但不应作为依据"的节点；**非现行被禁版本命中 0**，程序可判定的错误或无效引用 0。真实运行三类样例齐备：完整样例（退出码 0，2 条发现 + 3 个现行节点引用）、补充样例（两轮闭环，退出码 0，引用全部来自第二轮检索结果）、扫描件（材料层拒绝，退出码 2，未发模型请求），记录都在 `logs/agent-runs/`。README 已按 M3 现状重写。

M3 已知限制（如实记录）：`qwen3.7-flash` 在补充案例上 5 次全失败、`qwen3.8-max` 一次通过，属模型行为方差；措辞检查是黑名单兜底；embedding 非逐分量确定，"可复现"绑定具体缓存文件（结果 JSON 记了 `cache_key`）；CLI 只在最后写一份运行记录，补充案例第一轮的追问事件不进档。下一步是 M4（工作台与演示）。

里程碑进度仅在 [Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md) 更新，避免再维护多套看板。
