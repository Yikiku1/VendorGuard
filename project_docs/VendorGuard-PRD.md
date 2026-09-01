# VendorGuard：供应商准入与采购例外协同平台 PRD

> 内容类型：产品需求说明
>
> 文档状态：一期 MVP 精简基线
>
> 目标：在固定 10 天期限内，以约 65 至 75 小时交付范围明确、可诚实演示的 VendorGuard MVP。
>
> 配套文档：[前期调研与需求分析](./VendorGuard-前期调研与需求分析.md)、[10 天计划](./VendorGuard-10天计划.md)

VendorGuard 面向制造企业采购团队。系统审查供应商准入材料和采购申请例外，执行确定性风险规则，检索制度与历史案例提供依据，并将风险路由给有权限的人工审批人。

## 产品目标

你需要交付两个相互关联、可追溯的案件流程：供应商准入决定供应商能否进入 AVL；采购例外决定某一次使用非 AVL 供应商的 PR 是否可以继续。每个结论都关联原始材料、规则或检索依据，最终决定由人工完成。

```text
供应商准入：材料 -> 事实 -> 规则命中 -> 引用证据 -> 人工决定 -> AVL/归档
采购例外：PR -> AVL 校验 -> 例外规则与证据 -> 人工决定 -> 单次放行/拒绝/到期
```

## MVP 范围

### 供应商准入输入

每个供应商申请接受以下演示材料：

1. 供应商基本信息表
2. 营业执照 PDF
3. 按供应商品类要求提供的质量证书 PDF
4. 报价单 XLSX 或 CSV
5. 可选的历史履约记录 CSV

一期只支持一套固定营业执照 PDF 模板、一套固定报价 XLSX 模板和一套固定履约 CSV 模板。系统明确拒绝未知模板，不实现 OCR，也不声称支持任意供应商材料。质量证书作为材料完整性输入和附件元数据保存，本期不实现通用证书解析。首次合作供应商可以没有历史履约记录，系统应标记数据不足并转人工复核。

### 采购例外输入

每个采购例外必须关联一条 PR，并包含：PR 编号、申请人、物料品类、数量、金额与币种、紧急程度、目标供应商、未使用 AVL 供应商的原因、期望有效期和附件。系统在创建例外时保存供应商资格快照，防止后续状态变化破坏审计解释。

### 输出

- 材料完整性结果和补件任务
- 结构化事实及其文件、页码、单元格或 CSV 行来源
- 规则命中、风险等级和审批建议
- 制度或历史案例的 RAG 引用
- PR 的 AVL 校验结果、例外审批要求和单次放行范围
- 通过、拒绝或补件的人工决定
- 追加式审计时间线和 Agent 运行轨迹

### 非目标

- 自动准入、自动签约、自动创建采购订单或自动付款
- 真实工商、征信、ERP、财务或生产系统集成
- 收货、发票匹配、付款结账、库存 MRP 或复杂预测模型
- OCR、独立重排模型、复杂评测大屏或真实消息队列
- 使用真实供应商和业务数据

## 用户角色与权限

| 角色 | 操作 |
| --- | --- |
| 采购专员 | 创建准入申请和 PR、上传材料、补件、发起例外、查看结果 |
| 采购经理 | 审批报价、履约和采购例外风险 |
| 质量经理 | 已完成领域定义；证书临期和关键物料审批延期实现 |
| 管理员 | 已完成领域定义；独立管理页面和管理动作延期实现 |

首版保留四个稳定角色代码，但只为采购专员和采购经理提供预置账号并验证访问控制。采购专员不能审批自己提交的准入或例外案件。质量经理、管理员和关键物料双人审批属于已定义但未实现能力，不得在演示或简历中写成已交付。

## 业务状态

状态必须按对象分别保存，不能使用一个 `status` 同时表示供应商资格、案件进度、审批结果和技术运行结果。

### 准入案件状态

```text
draft -> pending_documents -> analyzing -> evidence_reviewing -> pending_approval
pending_documents -> analyzing
pending_approval -> approved | rejected
approved | rejected -> archived
```

### 供应商资格状态

```text
candidate -> approved | rejected
approved -> suspended | expired
suspended | expired -> approved | rejected
```

只有当前有效且资格为 `approved` 的供应商进入 AVL。

### 采购例外案件状态

```text
draft -> pending_approval -> approved | rejected
approved -> expired | archived
rejected -> archived
```

例外批准只允许关联 PR 在批准范围和有效期内继续，不改变供应商资格，也不能被其他 PR 复用。

### 运行与证据状态

工作流运行使用 `queued`、`running`、`succeeded`、`failed`；证据充分性使用 `sufficient`、`insufficient`。解析、检索或模型调用异常记录 `node_failed`，并允许从失败节点重试；完成检索但没有足够依据时记录 `evidence_insufficient`。两者不得混用。

## Agent 和工具职责

| 组件 | 输入 | 输出 | 约束 |
| --- | --- | --- | --- |
| 确定性编排图 | 案件状态、材料、节点结果 | 路由、待办、摘要 | 路由由状态和规则决定，不交给大模型自由决策 |
| 供应商分析 Agent | 解析后的事实、来源和规则命中 | 结构化风险摘要 | 不替代确定性解析、计算或准入决定 |
| 证据 Agent | 事实、规则命中、知识库 | 引用、解释、缺证据标记 | 不虚构条款或案例 |
| 确定性工具 | 文件、字段、策略 | 解析结果、计算结果、规则命中 | 处理计算、校验和权限 |
| 人工审批 | 风险和证据 | 批准、拒绝或补件 | 仅有权限角色可操作 |

LangGraph 负责确定性状态编排，供应商分析 Agent 和证据 Agent 作为受控节点运行。Agent 采用 Pydantic 结构化输出。每次运行保存状态、耗时、输入引用和输出引用；不默认复制保存整份敏感原文或完整 Prompt。

## 规则与审批矩阵

策略保存为版本化 YAML。七条规则继续作为领域设计保留，本期只实现并验证 `VEN-001`、`VEN-002`、`VEN-004`、`VEN-005` 和 `PR-001`。`VEN-003` 与 `VEN-006` 标记为延期实现，不进入 10 天完成判定。

```yaml
- id: VEN-001
  when: business_license_document_status in [expired, inconsistent, unreadable]
  action: block

- id: VEN-002
  when: category_required_documents_complete == false
  action: request_documents

- id: VEN-003
  when: quality_certificate_remaining_days < 90
  action: require_approval
  required_roles: [quality_manager]

- id: VEN-004
  when: quote_is_comparable == true and quote_deviation_percent > 20
  action: require_approval
  required_roles: [procurement_manager]

- id: VEN-005
  when: delivery_history_is_sufficient == true and on_time_delivery_rate < 90
  action: require_approval
  required_roles: [procurement_manager]

- id: VEN-006
  when: category == critical_material
  action: require_approval
  required_roles: [procurement_manager, quality_manager]

- id: PR-001
  when: supplier_is_in_avl == false
  action: require_exception_approval
  required_roles: [procurement_manager]
```

`VEN-001` 只表示上传材料已过期、字段矛盾或无法判读；未接入工商或认证机构数据源时，系统不得声称已经验证主体或证照真实性。`VEN-002` 的必填材料按供应商品类配置。`VEN-004` 只在币种、单位、含税口径和基准日期统一后计算。没有足够履约样本时不触发 `VEN-005`，而是生成“历史数据不足”人工复核项。`PR-001` 使用当前 AVL 投影判断是否需要例外；创建例外时仍保存供应商资格、有效期和暂停信息快照，用于审计解释。

规则阈值是教学模拟值。系统将规则命中、审批任务、人工决定和最终案件结果分开保存。延期规则仍保留测试设计，但未实现的双人审批和豁免不能出现在本期交付声明中。

## RAG 要求

RAG 为风险提供制度与历史案例依据，不负责做准入决定。

```text
文档解析 -> 分块与元数据 -> PostgreSQL 全文检索 + pgvector 向量召回
-> 结果融合 -> 带来源片段 -> Agent 引用输出
```

每个分块至少保存文档版本、生效期、章节或页码和原文。无证据时，系统输出“证据不足”。一期固定使用 5 篇相关制度、SOP 和模拟案例，实现 PostgreSQL 全文召回、pgvector 向量召回和简单 RRF 融合，并用 5 至 8 个固定问题验证引用。权限过滤、增量索引、独立重排和通用检索指标不进入一期。

## 核心数据实体

| 实体 | 用途 |
| --- | --- |
| `suppliers` | 保存供应商主体和当前资格；不保存案件运行状态 |
| `admission_cases` | 保存一次供应商准入申请及流程状态 |
| `purchase_requisitions` | 保存 PR 的物料、数量、金额、申请人与目标供应商 |
| `procurement_exception_cases` | 保存一次 PR 例外的原因、资格快照、批准范围和有效期 |
| `documents` | 保存上传文件、哈希和解析状态 |
| `extracted_facts` | 保存事实、置信度和材料来源 |
| `risk_findings` | 保存规则命中、风险状态和证据引用 |
| `policy_versions` | 保存 YAML 规则版本与生效时间 |
| `approval_tasks` | 保存分配给采购经理的待办及状态 |
| `decisions` | 保存批准、拒绝或补件决定和理由 |
| `rag_documents`、`rag_chunks` | 保存知识库文档、分块和检索元数据 |
| `workflow_runs`、`workflow_node_runs` | 保存工作流及解析、检索、Agent 节点的运行轨迹 |
| `audit_events` | 追加保存人工和系统操作 |

AVL 不单独维护一份可随意编辑的名单，而是从当前有效且资格为 `approved` 的供应商生成查询投影。

## 系统架构

### 架构原则

一期采用前后端分离的模块化单体，不拆微服务。这样既能保持业务模块边界，也能让事务、部署、调试和演示保持简单。

1. **业务流程优先**：准入、采购例外、审批和审计是产品核心；RAG 只是证据能力。
2. **确定性优先**：状态迁移、规则计算、权限、审批矩阵和到期判断由普通 Python 代码执行。
3. **人工最终决定**：Agent 可以整理事实和解释风险，不直接批准供应商或采购例外。
4. **证据可追溯**：事实、规则命中、引用、人工决定和运行轨迹使用稳定 ID 关联。
5. **追加式审计**：审计事件只追加；业务记录需要修正时创建新版本或新事件。
6. **失败可恢复**：准入工作流节点保存输入引用、输出引用和运行结果，并用一个可控失败场景验证从失败节点继续。
7. **依赖按需引入**：只有进入当前迭代的能力才增加依赖，不复制 `flo_prj` 的完整依赖集合。

### 运行视图

```text
React Web
   |
   v
FastAPI HTTP 层
   |
   v
准入 / 采购例外 / 审批等业务模块
   |              |               |
   v              v               v
规则与权限     确定性工作流      证据检索与受控 Agent
   |              |               |
   +--------------+---------------+
                  |
                  v
PostgreSQL + pgvector / 文件存储 / 追加式审计
```

HTTP 层只负责请求校验、身份解析、调用业务接口和转换响应。业务模块不能依赖 FastAPI 请求对象；数据库、文件、Embedding 和聊天模型位于模块内部 seam 后，由生产适配器和测试适配器提供实现。

### 核心请求流

```text
请求 -> Pydantic 校验 -> 身份与权限 -> 业务用例 -> 事务提交
     -> 审计事件 -> HTTP 响应

运行案件 -> 读取已完成节点 -> 确定性路由 -> 解析/规则/检索/Agent 节点
         -> 保存节点结果 -> 创建审批任务 -> 等待人工决定
```

## 技术栈

| 层级 | 选择 | 一期用途与约束 |
| --- | --- | --- |
| 运行环境 | Python 3.13、`uv` | 管理虚拟环境、依赖和锁文件；提交 `uv.lock` |
| HTTP 后端 | FastAPI、Uvicorn、Pydantic v2 | REST 接口、请求响应 Schema 和结构化 Agent 输出 |
| 配置 | `pydantic-settings` | 校验环境变量；密钥只放 `.env`，仓库只提交 `.env.example` |
| 数据访问 | SQLAlchemy 2.x Async、`asyncpg`、Alembic | 异步事务、PostgreSQL 连接池和数据库迁移；不使用全局长连接 |
| 数据库 | PostgreSQL 16+、pgvector | 同时保存业务数据、审计、全文索引和向量；一期不引入第二套向量数据库 |
| 认证授权 | PyJWT、`pwdlib[argon2]`、FastAPI Dependencies | 两个预置用户、JWT 和提交人与审批人隔离；不使用 MD5 或硬编码密钥 |
| 规则策略 | YAML、Pydantic、普通 Python | 规则版本和审批矩阵配置化；仅支持白名单字段与运算符，禁止使用 `eval` |
| 文件解析 | `pypdf`、`openpyxl`、Python `csv` | 各支持一套固定演示模板并保留页码、单元格和行号；一期不做 OCR |
| Agent 编排 | LangGraph | 只编排准入流程并验证一个恢复场景；大模型不决定路由或最终审批 |
| 模型接入 | OpenAI-compatible 模型适配器 | 延迟初始化、超时和结构化输出；业务模块不直接依赖厂商 SDK |
| RAG | PostgreSQL 全文检索、pgvector、简单 RRF | 基于 5 篇资料的混合召回、引用定位和无证据拒答 |
| 前端 | React、TypeScript、Vite | 构建登录、案件队列和统一案件详情三个入口 |
| 前端数据与表单 | React Router、原生 `fetch` | 三个页面的路由、请求与基础表单；本期不增加复杂状态库 |
| 前端界面 | Lucide Icons、项目内 CSS | 工作台式界面和一致的基础控件；不自建图标系统 |
| 测试 | pytest、pytest-asyncio、HTTPX、Playwright | 规则单测、接口集成测试和一条正常准入浏览器冒烟流程 |
| 代码质量 | Ruff、mypy | 格式、静态检查和类型检查；CI 中统一执行 |
| 可观测性 | Python `logging`、`ContextVar`、JSON Formatter | 记录 `request_id`、`case_id`、`workflow_run_id`，不记录密码和完整敏感原文 |
| 交付 | Docker Compose、GitHub Actions | 启动前端、后端和 PostgreSQL；自动执行检查与测试 |

所有具体版本在首次安装时由 `uv.lock`、前端锁文件和 Docker 镜像标签固定。除 PostgreSQL、模型接口和浏览器外，一期不引入 Redis、消息队列、Elasticsearch、Chroma 或独立微服务。

## 项目结构

目标结构如下。只有包含多种模型、接口和工作流的业务能力才建立目录；配置、数据库、认证、日志、审计和外部接入先使用单文件。文件在对应功能开始实现时创建，不提前铺满空的 `routes.py`、`models.py` 或 `repositories.py`。

```text
VendorGuard/
├─ src/vendorguard/
│  ├─ app.py                        # FastAPI 创建与路由装配
│  ├─ config.py                     # 经校验的运行配置
│  ├─ database.py                   # SQLAlchemy Engine 与 Session
│  ├─ security.py                   # 登录、JWT、角色与权限
│  ├─ logging.py                    # JSON 日志与 request_id
│  ├─ audit.py                      # 追加式审计写入与查询
│  ├─ suppliers.py                  # 供应商主体、资格与 AVL
│  ├─ storage.py                    # 本地文件存储适配器
│  ├─ llm.py                        # 模型与 Embedding 适配器
│  ├─ admission/                    # 准入案件、材料、事实和工作流
│  ├─ purchase_exceptions/          # PR 与单次采购例外
│  ├─ approvals/                    # 采购经理审批任务与决定
│  ├─ policy/                       # YAML 规则、审批矩阵和版本
│  └─ evidence/                     # 知识入库、混合检索和引用
├─ tests/
│  ├─ unit/                         # 规则、状态、计算和权限
│  ├─ integration/                  # PostgreSQL 与 HTTP 接口
│  ├─ e2e/                          # 正常准入浏览器冒烟流程
│  └─ fixtures/                     # 测试数据构造器
├─ policies/
│  ├─ rules/                        # 跨准入与采购例外的版本化规则
│  └─ approval/                     # 审批矩阵版本
├─ data/
│  ├─ demo/cases/                   # 三个演示案例及材料
│  ├─ knowledge/
│  │  ├─ policies/                 # 制度与 SOP
│  │  └─ cases/                    # 脱敏模拟历史案例
│  └─ evals/                        # 规则、检索和工作流评测集
├─ frontend/                        # React 应用
├─ project_docs/                    # PRD、调研、计划和 ADR
├─ alembic/                         # 数据库迁移
├─ compose.yaml
├─ pyproject.toml
└─ README.md
```

### 模块职责与依赖规则

| 模块 | 对外提供的主要能力 | 不负责 |
| --- | --- | --- |
| `suppliers.py` | 创建供应商、查询资格、生成 AVL 投影 | 准入案件运行和例外审批 |
| `admission` | 创建准入案件、接收材料、运行审查、应用最终决定 | 用户登录、通用审批策略和模型初始化 |
| `purchase_exceptions` | 创建 PR、校验 AVL、创建和关闭单次例外案件 | 改变供应商资格 |
| `approvals` | 生成采购经理审批任务、记录决定、校验提交人与审批人隔离 | 计算报价或检索制度 |
| `policy` | 加载版本化规则、计算命中、生成审批要求 | 调用大模型作最终判断 |
| `evidence` | 文档入库、混合检索、引用与拒答 | 改写业务事实或案件状态 |
| `security.py` | 登录、JWT、角色和权限上下文 | 保存业务案件 |
| `dependencies.py` | 提供可复用的 HTTP 认证依赖 | 保存业务状态或实现业务规则 |
| `audit.py` | 追加并查询审计事件 | 更新或删除历史事件 |

`app.py` 只装配 HTTP 路由和应用生命周期，业务规则不能写入其中。业务包可以调用根目录中的基础文件，但不能依赖其他业务包的内部文件。跨业务调用通过对方 `__init__.py` 暴露的少量函数完成，不允许直接修改其他模块拥有的数据表。只有当某个单文件已经出现多种独立职责或明显难以测试时才拆成目录。

## 工程质量门槛

- 每次提交前运行 Ruff、mypy 和相关 pytest；主分支 CI 必须通过。
- 数据库结构只通过 Alembic 迁移变更，不在应用启动时临时建表。
- 五条启用规则、关键状态迁移、提交人隔离和例外到期逻辑必须有正向、反向与边界测试。
- HTTP 错误返回稳定错误码和 `request_id`，不向客户端返回 `str(exception)`。
- 密码、Token、API Key、完整文档原文和个人信息不得写入日志。
- 外部模型调用必须设置超时；失败保存节点状态并可重试，不能把技术失败记录成“证据不足”。
- 三个简化案例必须使用固定种子数据，可在新环境中重复得到相同结果。
- README 必须包含安装、配置、迁移、启动、测试、演示账号和数据免责声明。

## 页面与接口

### 页面

1. 登录页：两个演示账号登录与错误提示
2. 案件队列：准入与采购例外待办、状态、风险数和更新时间
3. 统一案件详情：材料来源、规则、证据、Agent 输出、审批、工作流轨迹和审计时间线

PDF 页码、表格单元格、CSV 行和知识库原文在统一案件详情中展示。管理员页面不进入本期；时间有余时可以增加只读系统追踪页，但不计入完成判定。

### API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/admission/suppliers` | 采购专员创建候选供应商 |
| `POST` | `/api/admission/cases` | 基于已有 `supplier_id` 创建供应商准入案件 |
| `POST` | `/api/admission/cases/{id}/documents` | 上传准入材料 |
| `POST` | `/api/admission/cases/{id}/run` | 启动或从失败节点恢复准入工作流 |
| `GET` | `/api/admission/cases/{id}` | 查询准入案件详情 |
| `POST` | `/api/admission/cases/{id}/decisions` | 提交批准、拒绝或补件决定 |
| `POST` | `/purchase-requisitions` | 创建 PR 并校验目标供应商是否在 AVL |
| `POST` | `/purchase-requisitions/{id}/exception-cases` | 为非 AVL 供应商创建例外案件 |
| `POST` | `/exception-cases/{id}/run` | 执行例外规则与证据检查 |
| `GET` | `/exception-cases/{id}` | 查询例外、资格快照、审批要求和有效期 |
| `POST` | `/exception-cases/{id}/decisions` | 提交例外批准或拒绝决定 |
| `GET` | `/cases/{case_type}/{id}/audit-events` | 查询指定案件的追加式审计记录 |
| `GET` | `/policies` | 查询规则版本 |
| `POST` | `/rag/search` | 调试检索和引用 |

重复提交审批必须被唯一约束或业务校验拒绝，避免产生重复决定或重复审计事件。通用幂等框架不进入一期。

## 演示案例

| 案例 | 输入 | 结果 |
| --- | --- | --- |
| 正常准入 | 品类要求材料齐全，材料日期有效，报价和履约正常 | 采购经理确认，供应商资格变为 `approved` 并进入 AVL |
| 补件与复核 | 缺质量证书；补齐后统一报价口径并识别 25% 报价偏离 | 创建补件，采购经理复核报价风险，并保留首次缺件命中 |
| 采购例外 | 紧急 PR 使用未准入供应商，并提交单次采购理由与有效期 | 采购经理审批；批准只对该 PR 生效且不改变供应商资格 |

## 评测与验收

| 指标 | MVP 目标 |
| --- | --- |
| 五条启用规则固定案例 | 结果与预期一致 |
| 5 至 8 个检索问题 | 返回预期引用或“证据不足” |
| 两个 Agent 结构化输出 | 固定测试输入通过 Schema 校验 |
| 指定节点恢复场景 | 已成功节点不重复执行 |
| 采购例外范围隔离 | 跨 PR 与过期使用均被拒绝 |
| 提交人与审批人隔离 | 采购专员不能审批自己的案件 |

MVP 完成时，正常准入可从前端走完创建、分析、审批和审计闭环；补件复核与采购例外至少通过接口流程验证。采购例外不能改变供应商资格或被其他 PR 复用；支持范围内的风险可定位材料与制度依据。Docker Compose 可以启动应用和数据库。

## 实施里程碑

具体每日任务、交付物和验收要求见 [VendorGuard 10 天计划](./VendorGuard-10天计划.md)。PRD 只维护稳定的阶段目标，避免与执行计划形成两套进度事实。

| 阶段 | 天数 | 阶段结果 | 退出条件 |
| --- | --- | --- | --- |
| M1 工程与数据基础 | Day 1-2 | 术语、案例、配置、日志、FastAPI、PostgreSQL、迁移和两个账号的基础认证 | 健康检查、迁移、登录和基础工程检查通过 |
| M2 两条业务闭环 | Day 3-4 | 最小案件、五条启用规则、采购经理审批、单 PR 例外和审计 | 使用结构化事实跑通三个简化案例 |
| M3 解析、证据与编排 | Day 5-7 | 三个固定模板、五篇资料混合检索、两个受控 Agent 和一个恢复场景 | 正常准入工作流稳定运行并可从指定失败节点恢复 |
| M4 前端闭环 | Day 8-9 | 三个工作入口、内联证据和一条浏览器冒烟测试 | Playwright 完成正常准入，接口测试覆盖补件和例外 |
| M5 评测与交付 | Day 10 | 小型评测、CI、Docker Compose、README、架构图和演示材料 | 新环境能够按 README 复现核心 Demo，关键缺陷关闭 |

## 后续维护方向

一期交付后，优先实现 `VEN-003`、`VEN-006`、质量经理流程、关键物料双审批和只读系统追踪页，再考虑管理员管理动作、更多文件模板、权限过滤、知识库增量索引、OCR、独立重排和评测大屏。真实 ERP、工商或认证机构数据集成应在权限、数据合规与失败恢复机制稳定后再评估。
