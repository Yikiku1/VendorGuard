# 如何继续开发 VendorGuard

VendorGuard 是一个教学用途的企业 AI 应用。它帮助制造企业处理供应商准入和采购申请例外，不替代采购、质量或法务人员的最终决定。

> 最后整理：2026-09-01
> 当前阶段：Day 1、Day 2 已完成；Day 3 进行中
> 当前结论：最小案件模型、材料元数据约束、追加式审计存储和创建供应商 HTTP 接口已经验证；下一步实现创建准入案件与审计事件的事务闭环

## 先读什么

开始任何产品设计、编码或测试前，先读以下文件：

1. `PROJECT_CONTEXT.md`：当前项目事实、边界、进度和接手方式
2. `project_docs/VendorGuard-状态数据字典.md`：五类状态、迁移条件和非法迁移边界
3. `project_docs/VendorGuard-PRD.md`：MVP 范围、系统架构、技术栈和验收标准
4. `project_docs/VendorGuard-前期调研与需求分析.md`：业务依据、术语和规则来源
5. `project_docs/VendorGuard-10天计划.md`：每日任务、交付物和完成判定

当需求、调研或当前代码不一致时，以用户最新确认的要求为准；然后更新对应文档，避免形成两套事实。

## 当前进度

已经完成：

- 正式名称、一期范围、长期愿景和产品边界已经统一
- PRD、前期调研与需求分析、10 天冲刺计划已经整理
- 精简的模块化单体目录已经建立
- 五类状态的数据字典、迁移条件和最小测试清单已经固化
- 正常准入、补件后复核、未准入供应商采购例外三个 YAML 案例已经定义并通过格式与内部一致性检查
- `project_docs/VendorGuard-规则测试表.md` 的四项建模选择已经确认，七条规则共定义 43 个唯一测试用例
- `policies/rules/v1.0.0.yaml` 已建立并通过 YAML 结构、白名单运算符/动作和三案例合同检查
- `pyproject.toml` 已配置 `src` 包构建、最小运行/开发依赖、Ruff、mypy 和 pytest；`uv lock`、`uv sync`、Ruff 与 mypy 已验证通过
- `src/vendorguard/config.py` 已实现安全默认配置和 `VENDORGUARD_` 环境变量前缀；`.env.example` 已建立
- `src/vendorguard/app.py` 已实现 FastAPI 应用工厂、`/health`、统一 404/500 JSON 错误响应以及 `X-Request-ID`
- 后端已通过 Uvicorn `--factory` 方式实际启动，PowerShell 访问 `/health` 返回 `status = ok`
- `src/vendorguard/logging.py` 已建立 `ContextVar` 请求上下文、`JsonFormatter` 和幂等日志器配置函数；正常与异常 HTTP 请求均写入带相同 `request_id` 的 JSON 日志
- Day 1 共 13 个自动化测试通过；Ruff lint、Ruff format 和 mypy 对 `src`、`tests` 全部通过
- PostgreSQL + pgvector 容器已在 `127.0.0.1:5433` 健康运行，数据目录使用 D 盘配置
- SQLAlchemy 异步 Engine、请求级 Session、事务上下文和应用生命周期已经建立；应用关闭时会释放连接池
- Alembic 首个迁移已经完成升级、回退和再次升级验证；模型与迁移使用同名显式 `user_role` 约束，`alembic check` 无新增操作
- 四个角色代码已经固化；Argon2 密码哈希、JWT 签发/解析及过期、伪造、缺少密钥等边界测试已经通过
- `demo.specialist` 与 `demo.manager` 使用环境密码完成真实幂等种子验证，首次创建 2 个账号，再次执行创建 0 个账号
- 用户名、密码和启用状态的认证核心已经实现并覆盖正确密码、错误密码和停用账号
- `/auth/login`、Bearer Token 认证依赖和 `/auth/me` 已实现，覆盖登录成功、错误凭据、缺失、伪造、过期 Token 和数据库用户确认
- `Supplier`、`AdmissionCase`、`Document` 和 `AuditEvent` 最小模型及四个连续 Alembic 迁移已经实现
- 材料元数据已保存类型、大小和 SHA-256；数据库通过 `(admission_case_id, sha256)` 唯一约束拒绝同案件重复材料
- `append_audit_event()` 和 `list_audit_events()` 已实现，审计事件使用稳定递增 ID 返回时间线
- PostgreSQL 触发器已验证会阻止审计事件 `UPDATE` 和 `DELETE`，业务写入函数只 `flush()`，可与后续业务变化共用事务
- `POST /api/admission/suppliers` 已实现，采购专员和采购经理可以创建供应商，统一社会信用代码唯一性由数据库约束保证
- `admission/routes.py` 使用 Pydantic v2 `ConfigDict` 和标准权限检查模式；`get_current_user` 已从 `app.py` 导出为可复用依赖
- 创建供应商业务函数位于 `admission/__init__.py`，使用 `flush()` 而非 `commit()`，事务边界由 HTTP 层控制

尚未开始：

- 根目录初始 Hello World `main.py` 已删除，后端统一从 `vendorguard.app:create_app` 启动；`README.md` 仍为空
- 创建准入案件和查询案件详情的 HTTP 接口尚未实现
- 供应商、案件和审计事件的事务闭环尚未实现
- 材料目前只有元数据模型和重复约束，尚未实现登记接口、材料清单查询或实际文件存储
- 准入案件状态迁移尚未实现；非法状态迁移测试也尚未开始
- 前端、规则引擎、实际演示材料、Agent、RAG 和完整业务/E2E 自动化测试尚未实现；案例文件仍为 `defined_only`

当前测试基线为 58 项通过和 1 条已知的 `StarletteDeprecationWarning`；Ruff lint、Ruff format、mypy、`git diff --check` 与 `alembic check` 均通过。数据库位于 `9be8c214387a (head)`。该警告来自 FastAPI `TestClient` 的第三方兼容提示，不影响当前验收。

不要因为目录或文档已经存在，就把对应功能视为已完成。

## Day 1 完成验收

2026-08-28 已基于同一份代码完成以下验证：

```powershell
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
```

对应结果为 13 个测试通过、Ruff lint 通过、15 个文件格式通过、15 个源文件类型检查通过。新终端使用以下命令启动成功：

```powershell
uv run uvicorn vendorguard.app:create_app --factory --host 127.0.0.1 --port 8000
```

`Invoke-WebRequest http://127.0.0.1:8000/health` 返回 `200`、`{"status":"ok"}` 和非空 `X-Request-ID`；服务端 JSON 日志中的 `request_id` 与响应头一致。并发 HTTP 测试证明两个请求的上下文不串线，异常测试证明内部异常原文不会进入响应或 VendorGuard JSON 日志。

pytest 仍会报告 FastAPI `TestClient` 与 HTTP 客户端相关的 `StarletteDeprecationWarning`。它不影响当前验收，不应根据警告盲装依赖；进入后续依赖维护时再核对官方兼容关系和锁文件。

## Claude Code 接手点（2026-09-01 更新）

新会话开始后，先完整阅读本文件和”先读什么”列出的四份项目文档，并继续遵循”新对话的协作方式”。当前可靠事实如下：

- Day 1 已完成并在 10 天计划中勾选；保留现有配置、`/health`、错误响应和日志模块。
- Day 2 已完成数据库容器、请求级 Session、应用生命周期、Alembic 基础、角色约束、Argon2、JWT、演示账号种子、认证核心和 HTTP 认证链路。
- 两个演示账号已经真实写入本地 PostgreSQL；种子重复执行返回 0，证明当前数据库中的幂等性。
- Day 3 已完成模型层和创建供应商 HTTP 接口：`Supplier`、`AdmissionCase`、`Document`、`AuditEvent`、四个迁移、同案件材料 SHA-256 去重、审计追加/查询、数据库级禁止更新/删除和 `POST /api/admission/suppliers`。
- Day 3 尚未完成创建案件 HTTP 接口、供应商+案件+审计的事务闭环、材料登记与详情查询、状态迁移和对应业务审计串联，因此 10 天计划中的 Day 3 保持未勾选。
- 当前数据库迁移为 `9be8c214387a (head)`。联合验收为 pytest 58 passed、Ruff lint/format 通过、mypy 31 个文件无问题、`git diff --check` 通过、`alembic check` 无新增操作。
- 唯一已知的非阻塞提示是 `StarletteDeprecationWarning`；不要仅根据警告安装 `httpx2` 或修改锁文件。

下一小步是”采购专员创建准入案件”：实现 `POST /api/admission/cases` 接口，在一个事务中创建供应商、案件和 `admission_case_created` 审计事件。案件必须绑定供应商 ID 和提交人 ID，默认状态为 `draft`。不要重复认证功能，也不要提前实现 Day 4 审批。

## 产品定位

**正式名称**：VendorGuard：供应商准入与采购例外协同平台

**长期愿景**：供应商全生命周期风险协同平台；一期交付不使用该愿景名称代替当前产品范围。

**一期范围**：供应商准入与采购申请例外审批。

系统将供应商材料、结构化事实、规则命中、制度/案例引用、人工决定和审计事件关联为可恢复的案件流程。供应商准入和采购申请例外是两个独立但关联的流程：准入决定供应商能否进入 AVL；采购例外只决定某一次 PR 是否可继续，不改变供应商准入资格。

```text
供应商准入：申请 -> 材料核验 -> 事实提取 -> 规则门禁 -> 证据审校 -> 人工审批 -> AVL/归档
采购例外：PR -> AVL 校验 -> 例外申请 -> 规则与证据 -> 人工审批 -> 单次放行/拒绝/到期
```

## 一期边界

必须实现：

- 供应商申请、材料上传、补件和一个可验证的失败节点恢复场景
- 采购申请、未准入供应商识别、例外申请和单次审批闭环
- 一套固定营业执照 PDF、一套固定报价 XLSX 和一套固定履约 CSV 的事实提取及来源定位
- 五条启用规则、采购经理审批、提交人与审批人隔离和追加式审计
- 基于 5 篇资料的 PostgreSQL 全文检索、pgvector、简单融合、引用和无证据拒答
- 两个受控 Agent、一个准入 LangGraph、三个简化案例、自动化测试和 Docker Compose

不进入一期：

- 真实工商、征信、ERP、财务、签约或支付系统
- 自动签约、自动下 PO、自动付款或自动批准供应商
- 收货、发票匹配、付款结账、库存 MRP 和复杂预测模型
- OCR、独立重排模型、复杂评测大屏和真实消息队列
- 通用文件模板、权限过滤、知识库增量索引和通用检索质量承诺
- 质量经理业务流程、关键物料双人审批、限时豁免和管理员页面
- 使用或上传真实供应商、合同、报价、联系人数据

## 角色与权限

| 角色 | 主要动作 | 约束 |
| --- | --- | --- |
| 采购专员 | 创建申请、上传材料、补件 | 不能审批自己的申请 |
| 采购经理 | 审批报价、履约和例外风险 | 不能与提交人是同一用户 |
| 质量经理 | 仅保留角色定义 | 本期不创建演示账号或审批流程 |
| 管理员 | 仅保留角色定义 | 本期不创建演示账号或管理员页面 |

## 领域状态与规则

不同对象使用独立状态，禁止将案件进度、供应商资格、运行结果和人工决定混在同一枚举中：

| 对象 | 状态 | 含义 |
| --- | --- | --- |
| 准入案件 | `draft`、`pending_documents`、`analyzing`、`evidence_reviewing`、`pending_approval`、`approved`、`rejected`、`archived` | 描述一次准入案件的流程进度与终态 |
| 供应商资格 | `candidate`、`approved`、`suspended`、`expired`、`rejected` | 描述供应商是否具备常规采购资格 |
| 采购例外案件 | `draft`、`pending_approval`、`approved`、`rejected`、`expired`、`archived` | 描述一次 PR 例外的审批结果和有效性 |
| 工作流运行 | `queued`、`running`、`succeeded`、`failed` | 描述解析、检索或 Agent 节点的技术运行结果 |
| 证据充分性 | `sufficient`、`insufficient` | 描述是否找到足以支持风险解释的制度或案例证据 |

完整定义以 `project_docs/VendorGuard-状态数据字典.md` 为准。证据尚未评估时使用空值，不增加 `not_evaluated`；准入审批可退回 `pending_documents` 补件；工作流重试创建新运行记录，不能覆盖旧的 `failed` 记录。

合格供应商池（AVL）是当前有效且资格为 `approved` 的供应商投影。采购例外批准后只允许关联 PR 在有效期和批准范围内继续，不能复用于其他 PR，也不会自动把供应商加入 AVL。限时豁免保留为后续领域设计，本期不实现。

七条规则均保留为领域设计，其中五条进入本期实现：

| ID | 条件 | 结果 | 本期状态 |
| --- | --- | --- | --- |
| `VEN-001` | 材料中的营业执照已过期、字段矛盾或无法判读 | 阻断或人工核验 | 实现并验证 |
| `VEN-002` | 按供应商品类要求的必填材料缺失 | 要求补件 | 实现并验证 |
| `VEN-003` | 质量证书剩余不足 90 天 | 质量经理复核 | 延期实现 |
| `VEN-004` | 币种、单位、税口径和基准日期可比时，报价高于基准价 20% 以上 | 采购经理复核 | 实现并验证 |
| `VEN-005` | 存在足够历史记录且准时交付率低于 90% | 采购经理复核 | 实现并验证 |
| `VEN-006` | 关键物料 | 采购经理和质量经理双审批 | 延期实现 |
| `PR-001` | PR 使用当前不在 AVL 的供应商 | 创建仅适用于该 PR 的例外审批 | 实现并验证 |

规则阈值是教学模拟值，必须配置化，不能写死在 Prompt 或业务代码中。系统只能核验上传材料的一致性与声明有效期；未接入权威数据源时，不得声称已验证营业执照、证书或供应商主体的真实性。首次合作且没有历史履约数据时，记录“历史数据不足”并转人工复核，不按低履约率处理。

## Agent、RAG 与人工边界

| 层级 | 职责 | 输出 |
| --- | --- | --- |
| 确定性工具 | 解析、日期/价格/履约计算、规则匹配、权限校验 | 可验证事实和规则命中 |
| 供应商分析 Agent | 汇总解析事实、来源和规则命中 | 结构化风险摘要 |
| 证据 Agent | 检索制度和案例，验证风险解释 | 引用、处置建议、缺证据标记 |
| 确定性编排图 | 路由状态、补件、审批和恢复 | 案件状态、待办、摘要 |
| 人工审批 | 批准、拒绝、补件和单 PR 例外 | 最终决定与理由 |

LangGraph 只编排供应商准入的固定节点，不让大模型自行决定流程路由。RAG 只回答“依据和建议是什么”，不决定“是否准入”。没有证据时返回“证据不足”；解析或模型调用异常则记录独立的节点失败事件。人工决定不覆盖历史规则命中，只追加决定和审计事件。

## 工程要求

- 采用 Python、FastAPI、PostgreSQL、pgvector、LangGraph 和 React 的组合；按实际阶段逐步引入依赖
- 每个 Agent 使用 Pydantic 结构化输出，并保存运行状态、耗时、输入和输出引用
- 每项风险关联原始材料证据和制度/案例证据
- 审计日志只追加，不更新或删除
- 文档分块保留版本、生效期、章节或页码；权限元数据与过滤延期实现
- 所有演示数据必须是自制、公开模板或完全脱敏数据

代码采用精简的模块化单体。`admission`、`purchase_exceptions`、`approvals`、`policy` 和 `evidence` 是五个业务包；供应商资格、认证、数据库、日志、审计、文件和模型接入先使用根目录单文件。只有单文件出现多种独立职责或明显难以测试时才继续拆分。HTTP 路由只负责协议转换，业务规则和状态不放在路由中。前端只交付登录、案件队列和统一案件详情三个入口。

当前只保留以下骨架，不提前铺设空的 `routes.py`、`models.py` 或 `repositories.py`：

```text
VendorGuard/
├─ src/vendorguard/
│  ├─ admission/
│  ├─ purchase_exceptions/
│  ├─ approvals/
│  ├─ policy/
│  └─ evidence/
├─ data/
│  ├─ demo/
│  ├─ knowledge/
│  └─ evals/
├─ policies/
├─ tests/
├─ frontend/
└─ project_docs/
```

`app.py`、`config.py`、`database.py`、`security.py`、`logging.py`、`audit.py`、`suppliers.py`、`storage.py` 和 `llm.py` 等文件只在实现对应功能时创建。

## 新对话的协作方式

- 始终使用中文，面向第一次独立开发完整项目的学习者解释。
- 每个步骤先讲“为什么做”，再讲“具体怎么做”。
- 涉及操作时写明目标文件、PowerShell 命令、预期结果和验证方法。
- 一次只推进一个可验证的小步骤，不一次贴出大量代码，也不跨天抢跑。
- 用户提出概念问题时先回答问题；未明确要求修改时，不直接改代码。
- 安装依赖、下载资源或大规模写文件前先说明影响和目标位置，优先使用 D 盘。
- 保留已有文档和用户改动；不要复制 `flo_prj` 中的 MD5、硬编码 JWT、同步 PyMySQL、异常原文泄露或庞大依赖集合。

## 下一步

三个原始演示案例已经完成定义，现按精简范围调整为：

1. `data/demo/cases/normal_admission.yaml`：正常准入
2. `data/demo/cases/supplement_review.yaml`：补件后复核
3. `data/demo/cases/procurement_exception.yaml`：未准入供应商的采购例外

这些文件仍是 `defined_only` 测试合同，尚未经过正式规则引擎执行验证，也尚未生成对应的 PDF、XLSX 和 CSV 材料。正常准入保留五条启用规则均不命中；补件案例只保留 `VEN-002` 历史命中与 `VEN-004` 采购经理复核；采购例外只保留 `PR-001` 和采购经理单人审批。

`VEN-001` 至 `VEN-006`、`PR-001` 的输入字段、运算符、阈值和边界样例继续保留；`policies/rules/v1.0.0.yaml` 明确列出五条本期启用规则与两条延期规则。Day 1 工程基础已经完成并验收。

2026-08-31 已完成 `/auth/login`、Bearer Token 认证依赖和 `/auth/me`，Day 2 全部验收通过。Day 3 已完成最小模型、迁移、材料重复约束和追加式审计存储；下一步实现采购专员创建供应商与准入案件的小闭环。

## 开发顺序

1. 使用结构化模拟事实跑通正常准入、补件和最小采购例外
2. 实现五条启用规则、采购经理审批、提交人隔离和追加式审计
3. 实现三种固定模板解析、确定性计算和来源定位
4. 用 5 篇资料实现全文检索、pgvector、简单融合、引用和无证据拒答
5. 用 LangGraph 串联两个受控 Agent，验证一个失败节点恢复场景
6. 完成三个前端入口、Docker Compose 和演示材料

## 交付标准

正常准入必须能从页面完整跑通；补件后复核和未准入供应商采购例外至少通过接口流程验证。采购例外批准仅对指定 PR 生效且可到期；支持范围内的风险能定位到材料和制度依据；五条启用规则、提交人隔离、检索拒答和一个状态恢复场景有自动化测试。质量经理、双人审批、豁免和管理员页面不属于本期完成条件。
