# VendorGuard

供应商材料审查 Agent：提交一份材料，Agent 读取、核对来源、执行准入规则、检索现行内部制度，
缺信息时追问，最后只交付**带引用的结构化初审报告**，由用户确认或要求重查。

面向 AI Agent / AI 应用开发的求职演示项目；产品取舍见 [PRD](project_docs/VendorGuard-PRD.md)。

## 当前状态（M4：可独立演示的审查工作台）

M4 已完成：同一条审查链路现在有两个入口——命令行和一个**受认证的单页工作台**。

```text
登录 -> 上传文本 PDF + 填写审查要求 -> 页面显示"审查中"
     -> Agent 读取材料 / 校验事实 / 检索制度 -> 追问时页面补充一次 -> 第二轮
     -> 结构化报告: 每条发现的事实、规则结果、材料来源(定位到 PDF 页码)与制度引用(展开原文)
     -> 确认报告 / 要求重查 -> 可创建关联的新运行, 旧失败与旧记录保持不变
```

工作台是原生 HTML/CSS/JS（无前端工程、无 CDN），Token 只放 `sessionStorage`；
界面是深色 / 亮色两套**平面**配色（1px 边框、4px 圆角，没有渐变和投影），默认跟随系统偏好，
顶栏按钮可手动切换（选择记在浏览器本地）；
所有按钮按服务端算出的 `allowed_actions` 显示，前端不复制状态转换规则；
记录存在本地忽略目录 `var/reviews/`（**演示证据，不是业务案件**：不接 `admission_cases`、
不调用人工准入决定接口、不修改案件或供应商状态）。

## 环境准备

- Python 3.13 + [uv](https://docs.astral.sh/uv/)；
- Docker（数据库用 `compose.yaml` 里的 PostgreSQL 容器，另需 pgvector 扩展，镜像已固定）；
- 复制 `.env.example` 为 `.env` 并填好：数据库密码、JWT 密钥、演示账号密码、模型配置。

**模型费用来源**：Agent 与 embedding 都走**阿里云百炼**的 OpenAI 兼容端点（`VENDORGUARD_LLM_*`、
`VENDORGUARD_EMBEDDING_*`），费用由使用者自己承担。正文向量有本地缓存
（`data/retrieval/cache/`，gitignore）：**缓存缺失时首次启动会调用付费 embedding 接口**建立缓存，
之后按"模型名 + 正文指纹"复用；查询向量每次现算（很便宜），不进缓存。

演示账号由种子命令幂等创建（只补缺失的，已存在的原样不动）：

```powershell
uv run --no-sync python -m vendorguard.seed
```

## 启动

```powershell
# 1) 数据库 (首次或重启机器后)
docker compose up -d database

# 2) 应用 (启动时装载规则与检索数据; 片段清单缺失/缓存损坏/模型配置缺失会直接启动失败)
uv run --no-sync uvicorn vendorguard.app:create_app --factory
```

浏览器打开 <http://127.0.0.1:8000/workbench>，用 `.env` 里的演示账号登录
（`demo.specialist` 采购专员 / `demo.manager` 采购经理）；另有一个演示与开发用的管理员账号
`admin`，口令由 `VENDORGUARD_DEMO_ADMIN_PASSWORD` 决定，**不配置时沿用 `demo.specialist` 的口令**。

> 演示账号只用于本机演示：任何对外环境都必须换成强口令，并且不要复用这里的示例账号。

**五分钟演示**（从登录到来源核对、失败重跑与边界说明，含每一步的预期结果）见
[演示脚本](project_docs/VendorGuard-五分钟演示.md)。

命令行入口仍然可用（同一套应用层与同一个 Agent 循环）：

```powershell
uv run --no-sync python -m vendorguard.agent data/demo/materials/license_complete.pdf "请检查这家供应商的材料"
```

## 样例材料（自制演示件，不是真实证照）

| 文件 | 用途 |
| --- | --- |
| `data/demo/materials/license_complete.pdf` | 完整材料：有效期与清单齐全，正常出具报告 |
| `data/demo/materials/license_missing_date.pdf` | 缺有效期截止日：Agent 追问 → 补充一次 → 第二轮 |
| `data/demo/materials/license_scanned.pdf` | 扫描件：材料层直接拒绝，**不发模型请求**（不做 OCR） |

## 真实验收（2026-09-16，模型 `qwen3.8-max`，全部从页面操作）

本轮六条本地记录（`var/reviews/`，gitignore，不进公开仓库）：

| 路径 | 结果 |
| --- | --- |
| 完整材料 | `completed`，1 轮（4 次模型请求）：2 条发现，制度引用 4 条取自本次检索 |
| 缺有效期 → 补充 | `completed`，**2 轮**（追问 3 次请求 + 报告 4 次请求）：第二轮重新读取、重新校验、重新检索；补充来源显示原文，材料来源定位第 2 页 |
| 无依据请求（环保检测报告） | `completed`：模型检索现行制度后写明"缺少支持该结论的制度依据"，不硬答 |
| 扫描件 | `failed`，**0 轮**：`material_scanned_unsupported`，未发模型请求，不提供重跑 |
| 模型端点不可达（真实基础设施失败） | `failed`，1 轮：`agent_run_failed`，1 次模型请求、0 次工具调用，标记可重跑 |
| 对上一条点"用原记录重跑" | `completed`：新 `review_id` + `retry_of_review_id`，旧失败记录保持不变 |

模型行为有波动：上表是按**实际跑到的结果**记录的，不代表每次都成功；历史记录里
`qwen3.7-flash` 曾在"缺字段追问"案例上连续失败，本项目的应对是如实记录、不用一次成功冒充稳定性。

## 实际限制（如实列出）

- **评测范围**：制度检索只用 PRD 选定的 8 题评测（正常题 3/3、需纠正前提题 2/2、非现行版本命中 0）；
  其余 7 题明确未覆盖，三道 `no_answer` 题仍需人工核对。**不能写成"8/8 全自动正确"**。
- **可复现性绑定缓存**：embedding 服务端输出不是逐分量确定的，同一批正文两次构建的向量有 1e-4 级差异，
  所以"同一查询的 Top-5 可复现"只对**固定的缓存文件**成立（结果 JSON 里记了 `cache_key`）。
- **措辞检查是黑名单兜底**：拦"准入批准/已写入"这类宣告，可能误伤免责表述，也可能漏掉改头换面的说法；
  真正的保证是结构字段核对与固定案例人工复核。
- **初审报告不是准入决定**：报告确认只表示用户看过并接受这份报告，要求重查只记录反馈；
  两者都不调用准入审批、不改案件状态、不更新合格供应商清单。
- **本地记录是演示证据**：单进程、单 worker 的文件存储；多进程锁、共享存储、多租户、对象存储、
  在线部署都不在本阶段范围内。
- **不做 OCR**：扫描件在材料层被拒绝；也不做多文件上传、长期多轮对话（只支持一次补充）、
  历史制度回放或外部法规检索。

## 测试与检查

```powershell
uv run --no-sync pytest -q -p no:cacheprovider tests/unit          # 单测, 不需要数据库
docker compose up -d database
uv run --no-sync pytest -q -p no:cacheprovider tests/integration   # 集成测试, 需要数据库
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy
```

当前基线：单测 382 通过、集成测试 113 通过，Ruff / 格式检查 / mypy 全绿。

## 文档导航

- 开发顺序与完成标准：[Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md)；当前代码事实见 [PROJECT_CONTEXT](PROJECT_CONTEXT.md)。
- 本阶段方案与边界：[M4 实施方案](project_docs/VendorGuard-M4实施方案.md)。
- 历史阶段：[M1](project_docs/VendorGuard-M1实施方案.md)、[M2](project_docs/VendorGuard-M2实施方案.md)、[M3](project_docs/VendorGuard-M3实施方案.md)。
