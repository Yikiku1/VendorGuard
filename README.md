# VendorGuard

面向供应商准入场景的材料审查 Agent。用户上传一份供应商材料后，系统完成 PDF 读取、
事实与来源核对、确定性规则检查、现行制度检索和缺失信息追问，最终交付一份可回查原文的
结构化初审报告。

项目用于展示 AI Agent / AI 应用工程能力。它不让大模型直接决定供应商是否准入：模型负责
选择工具和组织解释，规则计算、来源校验、引用放行、状态转换和失败处理由程序控制。

## 30 秒了解项目

```text
登录 -> 上传文本 PDF + 审查要求
     -> 读取材料并定位页码
     -> 提取事实并核对原文
     -> 执行确定性准入规则
     -> 检索指定日期下的现行制度
     -> 缺信息时追问一次并重新执行完整链路
     -> 输出带材料来源和制度原文的初审报告
     -> 用户确认、要求重查或基于失败记录发起关联重跑
```

当前交付包括命令行入口和受认证的单页工作台。工作台支持审查历史、材料回看、制度引用展开、
工具轨迹、一次补充、报告反馈和失败重跑。报告是审查证据，不是准入决定；确认或重查都不会
修改供应商、案件或合格供应商清单状态。

## 核心设计

### 1. Agent 负责不确定判断，程序守住业务边界

- `read_material`、`check_materials`、`search_policy` 是业务工具；`ask_user` 和
  `submit_report` 是唯一追问与结论出口，纯文本不能直接结束审查。
- 日期有效性、材料完整性和规则命中由普通 Python 代码计算，不接受模型自行生成状态枚举。
- 工具白名单、调用预算、参数 Schema、超时和纠正次数由循环统一控制，预算耗尽时明确失败。
- 报告不得宣告准入批准；引用必须来自本次检索实际返回的节点，否则拒绝提交并要求模型改写。

### 2. 来源可以回到材料页和制度原文

- PDF 按页提取文本，事实必须在声明的材料来源中命中；用户补充与原始材料分开记录。
- 制度解析保留章节、表格行、版本、生效日期和原文定位，检索前先过滤非现行版本。
- 页面展开的是运行时保存的制度原文，不会在查看历史记录时重新检索并改变证据。
- 检索失败、依据不足和业务规则未通过是三种不同状态，不互相冒充。

### 3. 失败可复现，重跑不覆盖历史

- 每轮保存脱敏后的工具参数、结果摘要、错误码、模型请求次数、Token 与耗时。
- 可重试失败会创建新的 `review_id`，并用 `retry_of_review_id` 关联原记录；旧失败记录保持不变。
- 扫描件在材料层直接拒绝，不发模型请求，也不会被标记为可重试的模型失败。
- 页面按钮来自服务端计算的 `allowed_actions`，前端不复制状态转换规则。

### 4. HTTP 与 CLI 复用同一条审查链路

- FastAPI 应用和 CLI 共用 `ReviewCommand`、`ReviewRuntime` 与同一个 `run_review()` 工具循环。
- HTTP 层使用 Bearer Token 认证，并按 `owner_user_id` 隔离审查记录；跨用户访问统一返回 404。
- 同一记录的更新在进程内串行化，JSON 使用同目录临时文件加 `os.replace` 原子替换。
- 工作台使用原生 HTML/CSS/JS，不依赖 CDN；Token 只保存在当前标签页的 `sessionStorage`。

## 技术栈

- **Agent / LLM**：OpenAI SDK 兼容接口、Function Calling、Pydantic v2、结构化输出
- **检索**：版本过滤、Embedding、精确余弦 Top-K、引用闸门、冻结评测集
- **后端**：Python 3.13、FastAPI、SQLAlchemy 2.0 Async、PostgreSQL、pgvector、Alembic
- **工程**：pytest、Ruff、mypy strict、uv、Docker Compose
- **前端**：原生 HTML/CSS/JavaScript

## 快速启动

### 环境要求

- Python 3.13 和 [uv](https://docs.astral.sh/uv/)
- Docker
- 阿里云百炼 OpenAI 兼容模型与 Embedding 配置

复制 `.env.example` 为 `.env`，填写数据库密码、JWT 密钥、演示账号密码和模型配置。Agent 与
Embedding 调用费用由使用者承担。正文向量缓存在忽略目录 `data/retrieval/cache/`；缓存缺失时，
首次启动会调用 Embedding 接口构建缓存。

```powershell
# 安装锁定依赖
uv sync --frozen

# 启动 PostgreSQL + pgvector
docker compose up -d database

# 幂等创建演示账号
uv run --no-sync python -m vendorguard.seed

# 启动应用
uv run --no-sync uvicorn vendorguard.app:create_app --factory
```

浏览器打开 <http://127.0.0.1:8000/workbench>，使用 `.env` 中配置的演示账号登录。默认提供
`demo.specialist`、`demo.manager` 和 `admin` 三种演示身份；账号只用于本地演示，对外环境必须
更换口令。

命令行入口使用同一套应用层和 Agent 循环：

```powershell
uv run --no-sync python -m vendorguard.agent `
  data/demo/materials/license_complete.pdf `
  "请检查这家供应商的材料"
```

## 演示材料与路径

| 材料 | 演示目标 | 预期结果 |
| --- | --- | --- |
| `license_complete.pdf` | 完整材料 | 生成带材料页码与制度引用的报告 |
| `license_missing_date.pdf` | 缺有效期 | 追问一次，补充后重新读取、校验和检索 |
| `license_scanned.pdf` | 扫描件 | 材料层拒绝，模型调用次数为 0 |

完整现场演示步骤见 [五分钟演示脚本](project_docs/VendorGuard-五分钟演示.md)。脚本覆盖登录、
正常报告、缺项补充、依据不足、失败记录和关联重跑。

## 已验证结果

### 当前现场检查

| 检查项 | 结果 |
| --- | --- |
| 单元测试 | `384 passed` |
| Ruff | 通过 |
| Ruff format check | 通过 |
| mypy strict | 通过 |

### M4 收尾验收

2026-09-16 使用 `qwen3.8-max` 从页面完成六条真实运行记录：

| 路径 | 结果 |
| --- | --- |
| 完整材料 | `completed`，1 轮，2 条发现，4 条制度引用 |
| 缺有效期 -> 补充 | `completed`，保留 2 轮，第二轮重新读取、校验和检索 |
| 无依据请求 | `completed`，明确返回依据不足，不硬答 |
| 扫描件 | `failed`，0 轮，未发模型请求 |
| 模型端点不可达 | `failed`，保留真实基础设施错误并允许重跑 |
| 关联重跑 | 新记录成功，旧失败记录不变 |

M4 收尾时完整集成测试为 `113 passed`。这是历史验收结果，不冒充本次现场重跑；模型输出也存在
方差，单次成功不代表跨模型或跨运行稳定。

### 制度检索评测

当前检索评测选取冻结数据集中的 8 题：

- 3 道正常题的必需依据全部进入 Top-5；
- 2 道需要纠正前提的边界题全部命中内部制度依据；
- 非现行禁用版本命中数为 0；
- 3 道 `no_answer` 题仍需人工核对语义支持关系。

因此本项目不宣称“8/8 全自动正确”。结果文件位于
`data/evals/rag_phase1_m3_8q_result.json`，其中保留每题 Top-5、版本信息和人工核对项。

## 运行检查

```powershell
uv run --no-sync pytest -q -p no:cacheprovider tests/unit
docker compose up -d database
uv run --no-sync pytest -q -p no:cacheprovider tests/integration
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy
```

## 当前边界

- 只支持文本型 PDF；扫描件不做 OCR，也不支持多文件材料包。
- 补充只允许一次，不提供长期多轮对话。
- Embedding 服务端输出存在微小数值波动，Top-5 可复现性绑定冻结缓存。
- 依据不足由结构化报告显式表达，但 `no_answer` 的语义支持关系仍需固定案例人工核对。
- 审查记录保存在本地忽略目录 `var/reviews/`，固定单进程、单 worker；不承诺多进程共享存储。
- 不提供在线部署、多租户、对象存储、后台队列、SSE/WebSocket 或生产 SLA。
- 初审报告不会调用准入审批，不修改案件状态，也不更新合格供应商清单。

## 目录导航

```text
src/vendorguard/          Agent、材料处理、规则、检索、HTTP 与工作台
tests/unit/               无外部服务的单元测试
tests/integration/        PostgreSQL、认证、HTTP 与工作台集成测试
data/demo/                自制演示材料与事实样例
data/knowledge/           版本化制度语料与快照
data/evals/               冻结评测集和结果
policies/                 确定性业务规则
project_docs/             PRD、状态字典、语料来源核验和演示脚本
```

- 当前代码事实：[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)
- 产品范围与边界：[VendorGuard-PRD.md](project_docs/VendorGuard-PRD.md)
- 领域状态定义：[VendorGuard-状态数据字典.md](project_docs/VendorGuard-状态数据字典.md)
- 语料来源核验：[VendorGuard-RAG一期官方语料来源核验.md](project_docs/VendorGuard-RAG一期官方语料来源核验.md)
- 现场演示步骤：[VendorGuard-五分钟演示.md](project_docs/VendorGuard-五分钟演示.md)
