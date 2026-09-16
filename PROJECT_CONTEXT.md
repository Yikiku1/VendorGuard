# VendorGuard 当前上下文

> 更新：2026-09-16。M3 已完成；**M4 已完成（M4-0 至 M4-7）**：审查链路接成一个受认证的单页工作台，六条真实验收记录齐备；项目定位为 AI Agent / AI 应用开发求职。

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
| 后续产品链路 | M4 已完成：`review_application.py`（共用装载与单轮执行）、`review_records.py`（本地记录：布局、原子写、所有权与状态机）、`review_routes.py`（受认证的发起/列表/详情/材料/补充/反馈/重跑接口）、`workbench/`（单页工作台）都已实现并有真实验收记录；检索事件保存本次返回条款的原文，页面展开引用时**不重新检索**；跨轮对话只支持一次补充 |

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

2026-09-15：M3 方案修订并提交（`8fa46cc`），M3-0 基线复核通过，M3-1、M3-2 完成。当时的唯一小功能是 M3-3：向量检索与版本过滤；详细接口、步骤与验收清单见 [M3 实施方案](project_docs/VendorGuard-M3实施方案.md)。全量 274 条仅作为已有解析器的实测基线；旧版与外部法规元数据用于过滤负例测试，不生成本期负例向量。查询向量与正文缓存同维校验，制度引用回指本次检索实际返回的 `KnowledgeNode`；报告的结构化事实字段和规则结果对应本次成功的 `check_materials`，自由文本是否受来源支持仍由案例人工核对。补充后重新检索。BM25 与全量索引只在闭环后针对具体漏检单独考虑；M3 仍不访问数据库、不用 MinerU 一类版面解析、不新增领域实体。

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

2026-09-15：M4 详细方案已定义，见 [M4 实施方案](project_docs/VendorGuard-M4实施方案.md)。M4 只把现有 CLI 审查链路接成受认证的单页工作台，不新增 Agent/RAG 能力，也不接案件、审批或 AVL。

2026-09-15 M4-1 完成：新增 `src/vendorguard/review_application.py`（`ReviewCommand`、`ReviewRuntime`、`build_review_runtime()`、`run_review_round()`）——启动时装载模型配置、规则与检索数据一次，运行时按命令新建会话并执行一轮；`agent.py` 的 CLI 改接它，`main()` 内延迟导入应用层以避免 `agent` 与 `review_application` 的模块级环，仓库仍只有 `agent.py` 一条 `run_review()` 工具循环。CLI 对外行为不变，两处内部改动如实记录：材料检查移到装载依赖之前（扫描件不再牵连检索装载），第二轮改为"补充追加进命令后由应用层新建会话"。证据：`tests/unit/test_review_application.py` 8 项 + `tests/unit/test_agent_cli.py` 5 项，全量单测 353 通过（343 + 8 + 2），Ruff、格式检查与 mypy 全绿；真实启动路径实跑两次——模型端点指向死端口时先打印"已装载 22 条现行制度片段"再退 1，扫描件退 2、未装载检索、未发模型请求。本步不涉及数据库，集成测试未跑。

2026-09-15 M4-2 完成：新增 `src/vendorguard/review_records.py`（525 行）——记录 Schema（`ReviewRecord`、`ReviewRoundRecord`、`ReviewFailure`、`ReviewFeedback`、`ReviewStatus`、列表用的 `ReviewSummary`）与 `LocalReviewStore`（`create`、`get_for_owner`、`read_material_bytes`、`mutate_for_owner`、`list_for_owner`）。契约按方案第 6 节：目录固定 `<root>/<review_id>/{material.pdf, record.json}`，`review_id` 只按 UUID 解析（路径片段按"记录不存在"处理）、上传文件名只作展示；`record.json` 带显式 `schema_version=1.0`、时间必须带时区，写盘先写同目录临时文件再 `os.replace` 原子替换（失败时旧记录不变并清理临时文件）；所有读写核对 `owner_user_id`，别人的、不存在的与非法 id 统一报 `ReviewNotFoundError`（路由据此统一 404）；同一 `review_id` 的"检查状态 + 写盘"在 `mutate_for_owner()` 的同一次临界区内（按 id 的 `threading.Lock`），原子替换之外再解决"丢失更新"；状态机 `running` → `completed`/`question`/`failed`，`question` 只能补充一次，只有 `completed` 能提交一次反馈且不可覆盖，重跑由 `create(retry_of_review_id=...)` 建新记录（资格判断留给 M4-4）。工具事件在建轮次与写盘两处执行 `sk-` 脱敏；`agent.py` 的脱敏函数公开为 `redact_secret` 供记录层复用同一条规则；`config.py` 新增 `review_data_dir`（默认 `var/reviews`），`.env.example` 与 `.gitignore` 同步。证据：新增 `tests/unit/test_review_records.py` 24 项与 `test_config.py` 2 项，全量单测 379 通过，Ruff、格式检查与 mypy 全绿；并发用例做过去锁对照（临时去掉串行锁该用例立刻失败），并做了一次真跨进程核对（进程 1 建档并保存一轮 answer，进程 2 另一个解释器读回轮次、报告、列表摘要与 2716 字节材料）。本步不涉及数据库，集成测试未跑。

2026-09-16 M4-7 完成（M4 收尾）：真实模型（`qwen3.8-max`）从页面跑通六条记录——完整材料（1 轮 4 次请求、2 条发现、4 条制度引用）、缺有效期补充（**2 轮**：追问 3 次 + 报告 4 次，第二轮重新读取/校验/检索，补充来源显示原文、材料来源定位第 2 页）、无依据请求（模型检索后写明缺少支持结论的制度依据）、扫描件（0 轮、材料层拒绝、不发模型请求）、模型端点不可达造成的真实失败（1 轮、`agent_run_failed`、可重跑；**如实说明这是基础设施失败而非模型行为失败**）、对失败记录点重跑（新记录带 `retry_of_review_id`，旧记录不变）。布局检查：桌面 1280 两栏无横向溢出；360 单栏、无页面溢出、按钮不换行（唯一被截断的是顶栏用户名，按设计用省略号）；交互期无 JS 错误。限制如实记录：内置浏览器的真实键鼠命中不生效（用页面内标准 API 驱动同一批处理器）、截图通道中途失效（窄屏改用程序化测量）。`README.md` 已按 M4 现状重写（环境、启动、样例、模型费用来源、六条验收记录、五条限制）并新增[五分钟演示脚本](project_docs/VendorGuard-五分钟演示.md)。全量 495 通过（382 单测 + 113 集成），Ruff、格式检查与 mypy 全绿。

M0–M4 到此收尾：后续工作由 PRD 与求职需要另行决定，不再有「当前唯一小功能」。

2026-09-16 M4-6 完成：`workbench/` 补齐四类交互，一处**保存证据**的缺口同时补上——`agent.py` 的 `search_policy` 工具事件现在带 `nodes`（`node_key`/`edition_key`/`title`/`locator`/`text`），因为页面展开制度引用必须看到本次保存的原文（方案 4.5），查看时不能重新检索；`chunk_key` 是召回编号，不进这份证据。前端（`app.js`）：**工具轨迹**按发生顺序渲染每一轮的 `tool_events`（名称、成功/失败、参数与结果摘要，长内容用原生 `details` 折叠，**出错的那一步自动展开**）；**材料来源**里用户补充直接显示原文、材料定位给"查看材料 第 N 页"按钮（带 Token 取 PDF Blob → `window.open(blob#page=N)` 让阅读器直接停在那一页）；**制度引用**用 `details` 展开标题、定位、版本与本次保存的原文；**一次补充**表单、**确认报告 / 要求重查**（响应里的 `business_state_changed=false` 会显示成"没有改动任何业务状态"）、**用原记录重跑**。所有动作只按服务端 `allowed_actions` 显示，前端不推导状态。

测试与验证：`tests/integration/test_workbench.py` 从 12 项增至 16 项（新增：页面交互锚点齐全、前端只调审查接口——源码里 `/api/` 路径集合恰为 `{"/api/reviews"}`、用保存的证据而非重新检索（`tool_events` + `detail.nodes` + `edition_key` + `createObjectURL` + `#page=` + `user_supplement`）、`details`/`summary` 折叠与出错自动展开、边界提示仍在）；`tests/unit/test_agent.py` 扩了一条断言核对事件里的 `nodes`。全量 495 通过（382 单测 + 113 集成），Ruff、格式检查与 mypy 全绿。浏览器实测**四条路径**（真实登录与真实接口，只有 Agent 那一轮用替身）：正常（报告 + 2 条可展开引用（标题/定位/版本/原文）+ "查看材料 第 1 页"（拦下 `window.open` 得到 `blob:…#page=1`）+ 轨迹 4 步默认折叠 + 只显示反馈动作）；补充（追问时只显示补充表单 → 提交后两轮保留、补充来源显示原文、只剩反馈动作）；无依据（0 条发现 + "依据不足: …" + 边界提示）；失败（失败事实带错误码与"可用原记录重跑" + 轨迹里出错步骤自动展开 + 重跑按钮 → 新记录带 `retry_of_review_id`，列表显示"重跑自 …"）。限制：内置浏览器的真实键鼠命中仍不生效（用页面内标准 API 驱动）、截图通道不稳定（本轮拿到一张桌面图）、**真实模型的页面全链路留给 M4-7**。

2026-09-16 M4-5 完成：新增 `src/vendorguard/workbench/`——`__init__.py`（`GET /workbench` 公开页面壳 + `GET /assets/workbench/{name}` 白名单静态资源，只服务登记过的文件名、未登记一律 404，两个响应都带 `Cache-Control: no-cache` 免得改样式被缓存骗到）与 `index.html` / `styles.css` / `app.js`。页面是原生 HTML/CSS/JS，不依赖 CDN；所有接口文本用 `textContent` 渲染、Token 只放 `sessionStorage`（401 清 Token 回登录）、一次操作只发一个请求且进行中禁用按钮、可执行动作读服务端 `allowed_actions`。**样式口径（用户要求）**：Linear 风——深色平面、1px 细边框、圆角一律 4px（`--radius`）、**零渐变零投影**、单一强调色；长文件名与错误消息 `overflow-wrap: anywhere`。布局：宽屏（≥1000px）主区域两栏（新建/状态/材料 与 轮次/报告），窄屏按 材料→状态→轮次→报告 堆叠，左侧历史栏固定宽度可滚动。

测试与验证：新增 `tests/integration/test_workbench.py` 12 项（页面壳公开且自包含、两个资源可取、**白名单 404**（含目录穿越写法）、数据接口仍要 Token、前端不拼 `.innerHTML` 且 Token 只放 sessionStorage、动作读 `allowed_actions`、样式无渐变无投影且圆角统一走 4px 变量、不硬编码示例记录、资源目录固定）。浏览器实测（内置浏览器；登录与上传都是真实请求，只有 Agent 那一轮用替身）：未登录只见登录区；真实登录 200 后显示用户名与空历史；**从页面上传完整样例 → 报告**（6 项事实、1 轮、2 条发现、固定边界提示、"可执行动作: 确认报告或要求重查"）；刷新后从历史点开同一报告；提交中按钮立即禁用并显示"审查中…"；宽屏两栏无横向溢出；360 px 单栏、无元素溢出、按钮不换行。浏览器检查抓到并修掉两个真实缺陷：`[hidden]` 被 `.app { display: grid }` 压过导致未登录也显示主区域；资源缺缓存控制导致改样式刷新不生效；另把主区域从单列改成宽屏两栏（方案 8.1）。全量单测与集成测试 491 通过，Ruff、格式检查与 mypy 全绿。限制如实记录：内置浏览器的真实键鼠命中不生效（改用页面内标准 API 驱动同一批处理器），窄屏截图拿不到（用程序化测量代替），**真实模型的页面全链路留给 M4-7**。

2026-09-16 M4-4 完成：`review_routes.py` 增加三个接口（707 行），**记录层一行未改**——M4-2 留下的 `with_supplement` / `with_feedback` / `available_actions` / `with_round(failure=...)` 刚好够用。`POST /api/reviews/{id}/supplements`（只有 `question` 且未补充过能补充一次，其他状态 409；原文原样保存、1..2000 字；保存后进 `running` 并重新读材料、重新校验、重新检索，结果追加为第二轮，第一轮追问保持不变）；`POST /api/reviews/{id}/feedback`（只有 `completed` 且未反馈，`decision` 只允许 `confirmed`/`recheck_requested`，备注 ≤1000 字；响应在记录视图上多一个 `business_state_changed=false`，只记报告层意见、不动业务状态）；`POST /api/reviews/{id}/reruns`（复制本地保存的材料与原记录请求/参考日期，创建新 `review_id` 并设 `retry_of_review_id`，旧记录不变）。**重跑资格与页面按钮同源**：判定用 `available_actions()` 是否含 `rerun`，即只有可重试的失败或已要求重查的完成记录能重跑，扫描件（`retryable=false`）被挡住；接口签名里没有任何模型、路径或状态参数。路由层抽出 `_run_first_round` / `_execute_round` / `_load_material` 三个共用助手，使发起与重跑走同一条执行路径；`_mutate_or_conflict` 把 `ReviewStateError` 统一映射成 409 `state_conflict`，因为 `mutate` 在锁内抛错、写盘在其后，被拒请求不落任何痕迹。

测试与验证：新增 11 项 HTTP 用例（补充保留两轮且第二轮带补充原文、补充只允许一次、补充原文长度校验、反馈只记一次且不改业务状态、非完成状态与非法 decision/超长备注、重跑新 id 与旧记录逐字段不变、非可重试失败与无重查的完成记录不能重跑、重跑忽略请求体里的自选参数、三个写接口的所有者核对）。单测 382 通过、完整集成测试 **97 通过**（含数据库），Ruff、格式检查与 mypy 全绿。真实 HTTP 实跑两条链路（真实登录、Agent 那一轮用替身）：追问→补充→报告（`question` → `supplement` → `completed`，两轮保留，动作由 `supplement` 变 `feedback`）；失败→重跑（`failed` + `retryable=true` → 反馈 `recheck_requested` 且 `business_state_changed=false` → 重跑得到新 id 与 `retry_of_review_id`，旧记录保持 failed）。**真实模型的 HTTP 运行仍留给 M4-7**。

2026-09-16 M4-3 完成：新增 `src/vendorguard/review_routes.py`（488 行）——`POST /api/reviews`（multipart：材料 ≤10 MB、`request_text` 去空白后 1..1000 字、`reference_date` 为 ISO 日期）、`GET /api/reviews?limit=1..50`、`GET /api/reviews/{id}`、`GET /api/reviews/{id}/material`（返回原始 PDF 字节）。所有接口走现有 Bearer Token，记录不存在或不属于当前用户统一 404；一轮 Agent 用 `run_in_threadpool()` 跑，不阻塞事件循环。响应模型 `ReviewDetail`（完整记录 + 服务端算出的 `allowed_actions`，不写进 record.json）与 `ReviewListItem`（列表只给 7 个摘要字段）。错误体统一为 `{"error": {code, message, retryable}, request_id}`：`unauthenticated`(401)、`invalid_request`(400)、`upload_too_large`(413)、`review_not_found`(404)；材料失败逐码映射（`material_scanned_unsupported` 等，`retryable=false`），Agent 失败统一 `agent_run_failed`（`retryable=true`，原因是 message，文案不解析）。**201 只表示记录已创建**：扫描件这类材料错误也留下 failed 记录并返回，表单/大小错误在建档前 4xx 且不建档。401 与校验错误处理器只对 `/api/reviews` 用统一错误体，`/auth/login` 的 `detail` 形状不变。记录层补齐三处能力：`with_round(..., failure=...)`（失败轮次必须同时给出 failure）、`available_actions()`、`material_path_for_owner()`（先核对所有者再给出材料路径）。`app.py` 的 lifespan 在数据库之外装载 `review_store` 与 `review_runtime`（配置或缓存不可用即启动失败），并注册审查错误处理器与路由；依赖新增 `python-multipart`。

测试与验证：新增 `tests/integration/conftest.py`（把启动时的审查依赖装载换成替身、记录目录指向临时目录，既有集成测试不再依赖模型配置与检索缓存）、`tests/integration/test_review_routes.py`（16 项，全离线：认证用依赖覆盖、runtime/store 与那一轮 Agent 用替身），记录层新增 3 项。单测 382 通过；**完整集成测试 86 通过**（Docker 与数据库启动后首次全量跑，含既有认证与案件用例）；Ruff、格式检查与 mypy 全绿。真实链路实跑（Agent 那一轮用替身，不消耗模型调用）：`demo.specialist` 登录 200 并取到真实身份、未认证 401、带 Token 发起审查 201（`completed`、`allowed_actions=["feedback"]`、材料身份 `material`、1 页）、详情与 201 响应逐字段一致、材料字节与上传一致、`demo.manager` 跨用户读取与材料均 404 且列表为空。**真实模型的 HTTP 运行留给 M4-7 验收**，本步不宣称已跑过。

里程碑进度仅在 [Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md) 更新，避免再维护多套看板。
