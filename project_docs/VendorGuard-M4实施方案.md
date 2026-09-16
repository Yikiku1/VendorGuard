# M4：形成可独立演示的审查工作台

> 日期：2026-09-15。
> 状态：M4-1、M4-2 已完成（2026-09-15），M4-3 待开始。
> 当前唯一小功能：M4-3 受认证的发起、列表和详情接口（记录见[开发计划](VendorGuard-Agent开发计划.md)）。
> 上游依据：[PRD](VendorGuard-PRD.md) 的第一版范围与完成标准、
> [Agent 开发计划](VendorGuard-Agent开发计划.md)和[M3 实施方案](VendorGuard-M3实施方案.md)。

## 1. M4 要交付什么

M3 已经能从命令行完成材料读取、确定性校验、制度检索、一次补充和结构化报告。
M4 不增加新的 Agent 能力，而是把这条链路接入现有 FastAPI，交付一个可以登录、操作、
核对来源并复现失败的单页工作台。

```text
登录
  -> 上传文本 PDF + 输入审查要求
  -> 同一审查应用层运行 Agent
  -> 页面展示本轮工具事件
  -> 若 Agent 追问, 用户补充一次并开始新一轮
  -> 页面展示结构化报告、材料来源和制度原文
  -> 用户确认报告或要求重查
  -> 失败记录保留, 可创建关联的新运行
```

最终产物仍是**初审报告**。确认报告只表示用户看过并接受这份报告，要求重查只记录反馈；
两者都不调用已有准入审批接口，不修改案件状态，也不更新合格供应商清单。

## 2. M3 交给 M4 的基线

M4 直接复用以下已实现能力：

- `run_review()` 是唯一 Agent 工具循环，`submit_report` 是唯一结论出口。
- `ReviewSession` 持有一份材料和补充原文；现有 CLI 把补充限制为最多一次，补充后的运行重新
  读取、校验和检索。M4 的 HTTP 状态转换继续显式执行这项一次限制。
- `AgentRunOutcome` 已包含结果种类、工具事件、token、耗时和结构化报告。
- `load_material()`、`load_policy_index()`、策略加载和 OpenAI 兼容客户端已在 CLI 中完成接线。
- 现有 FastAPI 已有 `/auth/login`、`/auth/me`、Bearer Token、请求 ID 和统一异常处理。

M4 开始前的现场基线是 `343 passed`，Ruff、格式和 mypy 通过。M3 的 8 题评测只证明
正常题 `3/3`、需纠正前提题 `2/2`、非现行版本命中 `0`；三道无答案题仍由人工核对，
不能在页面或文档中写成“8/8 全自动正确”。

## 3. 范围

### 本阶段实现

- 一个供 CLI 与 HTTP 共用的审查应用层，不复制 `run_review()` 或工具分发逻辑。
- 本地审查记录：保存上传材料、每轮结果、工具事件、报告、反馈和重跑关系。
- 受现有 Bearer Token 保护的审查 HTTP 接口；每个用户只能读取和操作自己的记录。
- 一个由 FastAPI 提供的单页工作台：登录、上传、追问补充、运行历史、工具轨迹、
  报告与来源查看、反馈和重跑。
- 正常、补充、无依据和失败四条页面验收路径；桌面与窄屏检查。
- 可复现启动说明、五分钟演示脚本和真实限制说明。

### 本阶段不实现

- 新 Agent、新业务工具、LangGraph、多 Agent、模型路由或提示词评测平台。
- WebSocket、Server-Sent Events（SSE）、后台队列、进程外任务调度或节点级恢复。
- React/Vite 等独立前端工程、前后端分仓、服务端渲染框架或新的 Node.js 工具链。
- 把审查记录写入现有业务数据库、接入案件/审批/AVL 状态或新增数据库迁移。
- 多文件上传、扫描件 OCR、长期多轮对话、任意历史制度回放或外部法规检索。
- 在线部署、多租户、对象存储、生产级并发承诺或实时 token 流式输出。

## 4. 五个必须守住的边界

### 4.1 CLI 与 HTTP 只能有一条审查执行链

把 CLI 里“加载模型配置、策略、检索索引并执行一轮”的接线移到审查应用层。
CLI 和 HTTP 都调用这个接口；`agent.py` 继续持有唯一工具循环。页面路由不得复制工具定义、
纠正预算、报告校验或补充逻辑。

### 4.2 HTTP 等待一轮完成，不引入后台任务

首版每次 `POST` 同步等待一轮 Agent 结束，FastAPI 使用线程池运行现有同步调用，避免阻塞
事件循环。浏览器在等待期间显示稳定的“审查中”状态，响应返回后一次性展示工具事件。
总时限仍由 M3 的 Agent 预算控制；本阶段不承诺实时轨迹。

### 4.3 本地记录是演示证据，不是业务案件

审查记录保存在本地忽略目录，使用独立 `review_id` 和 `owner_user_id`。它不引用或推进
`admission_cases`，也不复用人工准入决定接口。报告确认与重查反馈属于报告层动作，
不能表述成采购经理批准或供应商准入。

### 4.4 重跑创建新记录，旧失败不可覆盖

补充是在同一个 `review_id` 下追加第二轮；失败重跑或用户要求重查后再次运行则创建新的
`review_id`，通过 `retry_of_review_id` 指向旧记录。旧记录、旧工具事件和旧错误保持不变，
页面可以对比前后运行。

### 4.5 来源必须能看到实际内容

页面不能只显示 `license_complete@page:1` 或 `node_key`。材料来源要能定位到 PDF 页码或
用户补充原文；制度引用要显示标题、定位路径和本次检索返回的原文。页面展示的是已保存的
本轮证据，不在查看时重新检索并冒充原始结果。

## 5. 模块与接口

M4 新增三个后端模块和一组静态资源；复杂行为集中在应用层与本地记录模块，路由只负责
HTTP 转换和认证。

| 文件 | 作用 | 主要接口 |
| --- | --- | --- |
| `src/vendorguard/review_application.py` | 统一装载 Agent 依赖并执行一轮；CLI 与 HTTP 共用 | `build_review_runtime(...) -> ReviewRuntime`；`run_review_round(command, *, runtime) -> AgentRunOutcome` |
| `src/vendorguard/review_records.py` | 审查记录 Schema、本地原子读写、所有权和状态转换 | `LocalReviewStore.create(...)`；`get_for_owner(...)`；`mutate_for_owner(...)`；`list_for_owner(...)` |
| `src/vendorguard/review_routes.py` | HTTP 输入输出、认证、上传限制、线程池调用和错误映射 | `router = APIRouter(prefix="/api/reviews")` |
| `src/vendorguard/workbench/` | 单页 HTML、CSS 与 JavaScript | `/workbench` 页面与 `/assets/workbench/*` 静态资源 |

修改现有文件：

| 文件 | 修改内容 |
| --- | --- |
| `src/vendorguard/agent.py` | CLI 改用 `review_application` 的共用接线；保留唯一 `run_review()` 与 CLI 入口 |
| `src/vendorguard/app.py` | 应用启动时装载审查运行依赖和本地存储，注册审查路由及工作台资源 |
| `src/vendorguard/config.py`、`.env.example` | 新增本地审查目录和上传大小配置 |
| `pyproject.toml`、`uv.lock` | 增加 FastAPI 文件上传所需的 `python-multipart` |
| `.gitignore` | 忽略本地审查材料与记录目录 `var/reviews/` |
| `README.md`、`PROJECT_CONTEXT.md`、开发计划 | 只在对应步骤验收后更新已实现事实 |

### 5.1 审查应用层接口

建议数据形状：

```python
class ReviewCommand(BaseModel):
    user_request: str
    reference_date: date
    material: MaterialDocument
    supplements: tuple[str, ...] = ()


def run_review_round(
    command: ReviewCommand,
    *,
    runtime: ReviewRuntime,
) -> AgentRunOutcome: ...
```

`ReviewRuntime` 持有 `client`、`policy`、`policy_index` 和 `model_name`。依赖由应用启动或 CLI
启动时创建一次；测试直接传替身。`run_review_round()` 每次根据 command 新建
`ReviewSession`，登记已有补充，再调用现有 `run_review()`。它不保存文件、不认识 HTTP，
也不改变 `AgentRunOutcome` 的业务语义。

### 5.2 应用启动行为

FastAPI lifespan 在数据库资源之外装载 `ReviewRuntime` 与 `LocalReviewStore`，分别写入
`app.state.review_runtime` 和 `app.state.review_store`。片段清单、embedding 缓存或模型配置
不可用时启动失败，不能让页面启动后把配置错误说成“制度无依据”。

测试可通过依赖函数覆盖 runtime 与 store，不要求真实模型、embedding 或本地数据库。

## 6. 本地审查记录契约

目录固定为：

```text
var/reviews/<review_id>/
    material.pdf
    record.json
```

`review_id` 由服务端生成 UUID；材料文件固定命名为 `material.pdf`，不使用上传文件名拼路径。
原始文件名只作为展示字段保存。`record.json` 使用 UTF-8 和显式 `schema_version`；更新时先写
同目录临时文件，再原子替换，避免进程中断留下半份 JSON。临时文件在失败后清理。

`LocalReviewStore` 在同一进程内按 `review_id` 串行修改记录；状态检查和写盘必须放在同一次
`mutate_for_owner()` 临界区内，避免两个补充或反馈请求同时通过检查后互相覆盖。原子替换只解决
“半份文件”，不能单独解决“丢失更新”。M4 本地演示固定使用单个 Uvicorn worker；多进程锁、
共享存储和分布式并发不在本阶段范围内。

### 6.1 `ReviewRecord`

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 首版固定 `1.0` |
| `review_id` / `owner_user_id` | 记录身份与所有者；所有读取和修改都核对所有者 |
| `retry_of_review_id` | 新运行来源；首次运行为空 |
| `created_at` / `updated_at` | 带时区时间 |
| `status` | `running`、`question`、`completed` 或 `failed` |
| `request_text` / `reference_date` | 原始审查要求与规则参考日期 |
| `original_filename` | 仅展示，不参与本地路径计算 |
| `material_id` / `material_sha256` / `page_count` | 材料层成功读取后的稳定身份 |
| `supplements` | 最多一条用户补充原文，保留 `round:1` 来源语义 |
| `rounds` | 每次 Agent 运行的完整结果，第一轮不能因补充而丢失 |
| `feedback` | 一次不可覆盖的报告反馈，可为空 |
| `failure` | 材料读取或 Agent 失败的稳定错误码、用户提示与 `retryable`；成功时为空 |

### 6.2 `ReviewRoundRecord`

每轮保存：轮次、开始/结束时间、模型、`kind`、用户可见文本、模型请求数、工具尝试数、
耗时、token 用量、脱敏后的工具事件和结构化报告。第二轮必须保存自己的检索返回和引用，
不能把第一轮节点登记带入第二轮。

工具事件复用 `AgentRunOutcome.tool_events`，但写盘前继续执行 M3 的 `sk-` 脱敏兜底。
浏览器响应不返回 API Key、请求头或环境变量；材料全文通过受保护的材料接口读取，
不复制进审查列表响应。

### 6.3 状态转换

```text
create -> running -> completed
                  -> question -> running -> completed
                                        -> failed
                  -> failed

completed -> feedback: confirmed
completed -> feedback: recheck_requested -> new review (retry_of_review_id)
failed ---------------------------------> new review (retry_of_review_id)
```

`question` 只允许一次补充。`completed` 才能提交反馈。反馈一旦保存不能覆盖；需要改变意见时
创建重查记录并留下关联。重跑复制已保存材料、原请求和参考日期，创建新记录后从第一轮重新执行。

## 7. HTTP 契约

所有 `/api/reviews` 接口使用现有 `get_current_user`。未认证返回 `401`；记录不存在或不属于
当前用户都返回统一 `404`，不泄露其他用户的记录是否存在。

详情响应额外返回服务端计算、但不写入 `record.json` 的 `allowed_actions`：可选值只有
`supplement`、`feedback` 和 `rerun`。工作台只按这个字段显示操作按钮，不在 JavaScript 中
复制状态转换规则。

### 7.1 发起审查

`POST /api/reviews`，`multipart/form-data`：

| 字段 | 约束 |
| --- | --- |
| `material` | 必填；最多 10 MB；只接受一份 PDF，最终仍由 `read_text_pdf()` 判定是否可读 |
| `request_text` | 必填；去首尾空白后 1 至 1000 字符 |
| `reference_date` | 必填 ISO 日期；演示使用 `2026-09-01` |

服务端最多读取 `10 MB + 1 byte` 后判断超限；不信任 MIME 或原始文件名。先创建运行记录并
保存材料，再在线程池执行应用层。成功返回 `201` 和完整记录视图；扫描件等材料错误也保留
一条 `failed` 记录，不发模型请求。

这里的 `201` 表示“审查记录已创建”，不表示 Agent 一定产出报告：只要记录已经落盘，接口就
返回 `201` 和完整记录视图，调用方根据 `status` 与 `failure` 展示 `completed`、`question` 或
`failed`。缺少表单字段、日期格式错误、上传超过限制等发生在建档前的请求错误，才返回
`4xx` 且不创建记录。这样失败历史不会因为 HTTP 错误分支而丢失。

### 7.2 查看历史与详情

- `GET /api/reviews?limit=20`：返回当前用户最近记录摘要，按创建时间倒序，`limit` 为 1 至 50
- `GET /api/reviews/{review_id}`：返回完整记录、所有轮次、工具事件、报告、反馈和 `allowed_actions`
- `GET /api/reviews/{review_id}/material`：返回原始 PDF；页面通过带 Token 的 `fetch` 取得 Blob

列表只返回 ID、文件名、状态、创建时间、请求摘要、报告发现数和重跑关系，不带全文或工具事件。

### 7.3 提交一次补充

`POST /api/reviews/{review_id}/supplements`：

```json
{"text": "营业执照有效期截止日为 2027-08-31"}
```

只有 `question` 状态且尚无补充时可调用，其他状态返回 `409`。补充原文长度为 1 至 2000 字符。
保存补充后进入 `running`，在线程池重新执行一轮；这一轮重新读取、校验和检索，完成后追加到
`rounds`，不覆盖第一轮追问。

### 7.4 反馈与重跑

`POST /api/reviews/{review_id}/feedback`：

```json
{"decision": "confirmed", "comment": "来源与报告内容已核对"}
```

`decision` 只允许 `confirmed` 或 `recheck_requested`，comment 最多 1000 字符。只有
`completed` 且尚无反馈时可提交；响应明确写 `business_state_changed=false`。

`POST /api/reviews/{review_id}/reruns` 不接收模型、路径或状态参数。仅允许 `failure.retryable`
为 true 的 `failed` 记录，或反馈为 `recheck_requested` 的完成记录。它复制保存的材料与原始
请求，创建新的 `review_id`，设置 `retry_of_review_id` 后重新运行；旧记录保持不变。扫描件等
不可重跑的材料错误不显示 `rerun`，用户只能用可读材料发起全新审查。

### 7.5 错误响应

无法接受请求、且没有创建或更新审查记录时，M4 接口统一返回：

```json
{
  "error": {
    "code": "material_scanned_unsupported",
    "message": "扫描件暂不支持，请提交带文本层的 PDF",
    "retryable": false
  },
  "request_id": "..."
}
```

错误码至少区分：认证失败、参数错误、上传过大和运行状态冲突。材料不可读、模型失败、
检索配置失败和预算耗尽发生在记录创建或轮次开始之后，写入记录的 `failure` 并随记录视图返回。
用户可通过原记录重跑时 `retryable=true`；扫描件在当前范围内为 false，但页面仍可用另一份
材料发起全新审查。

## 8. 工作台信息架构

工作台是操作界面，不做营销首页。`GET /workbench` 返回公开页面壳；未登录只显示登录区域，
数据接口始终要求 Bearer Token。Token 只放 `sessionStorage`，页面刷新后可恢复当前标签页会话；
收到 `401` 时清除 Token 并回到登录状态。

### 8.1 桌面布局

```text
┌──────────────────────────────────────────────────────────────────────┐
│ VendorGuard  当前用户                         新建审查      退出     │
├────────────────┬─────────────────────────────────────────────────────┤
│ 最近审查       │ 材料与请求          运行状态 / 补充输入              │
│                ├─────────────────────────────────────────────────────┤
│ 状态 + 文件名  │ 工具轨迹            结构化报告                       │
│ 时间 + 关联    │ read/check/search   findings + 来源展开              │
│                │ submit_report                                        │
└────────────────┴─────────────────────────────────────────────────────┘
```

左侧历史栏固定宽度并可滚动；主区域使用上下分区，报告与轨迹在宽屏并列、窄屏按材料、
状态、补充、报告、工具轨迹顺序堆叠。界面以扫描和重复操作为主，避免营销式 Hero、装饰渐变和
页面区块卡片化。

### 8.2 关键界面状态

页面必须实现：未登录、空历史、文件待提交、上传/审查中、等待补充、报告完成、依据不足、
失败、反馈已提交和重跑关联。同一次页面操作只发送一个请求，请求进行中立即禁用对应按钮；
可执行动作以详情响应的 `allowed_actions` 为准。

报告区域逐条显示：

- finding 摘要
- 已核对事实和规则结果
- 材料来源按钮：打开 PDF Blob 并定位页码；用户补充来源直接显示原文
- 制度引用按钮：展开制度标题、定位路径和本次保存的原文
- 依据不足原因和“初审报告不是准入决定”的固定边界提示

工具轨迹按发生顺序显示名称、成功/失败状态、参数摘要与结果摘要。默认折叠长内容，错误步骤
自动展开。不要显示隐藏思维过程，也不要把工具事件改写成模型推理说明。

### 8.3 前端实现约束

使用原生 HTML、CSS 和 JavaScript；调用浏览器 `fetch`、`FormData`、`sessionStorage` 与
Blob URL。页面资源放在 Python 包内，由 FastAPI 提供，不依赖 CDN。所有用户输入用
`textContent` 渲染，不拼接 `innerHTML`。动态内容提供加载、空、失败和禁用状态；最长文件名、
节点定位和错误消息在桌面及 360 px 宽度下不得溢出或遮挡操作。

## 9. 实施步骤

每一步交付一个可运行结果，后一步不得提前复制未稳定的接口。

### M4-0：冻结 M3 基线并修正文档

状态：已完成（2026-09-15），属于 M4 开工准备，不代表 M4 功能已经实现。

已运行 343 项单测、Ruff、格式和 mypy；复跑 8 题评测只核对汇总，时间戳和浮点末位的
非确定差异未提交。已修正 `PROJECT_CONTEXT.md` 中“M3 制度检索与报告尚未实现”的旧句子。

完成判据：基线检查通过，工作区无评测复跑噪声，文档对 M3 状态没有矛盾。

### M4-1：抽出共用审查应用层

状态：已完成（2026-09-15）。应用层接口、CLI 改接、两处有意改变与实测证据见
[开发计划](VendorGuard-Agent开发计划.md) 的 M4-1 完成记录。

修改范围：`review_application.py`、`agent.py`、对应单元测试。

先写测试：runtime 缺配置时一次报清；完整依赖由外部注入；同一 `ReviewCommand` 能返回
`question`、`answer + report` 和 `failed`；CLI 改接应用层后仍只调用现有 `run_review()`。

完成判据：CLI 的完整、缺日期和扫描件行为不变；HTTP 后续只需传 command，不需要理解
工具定义、预算、检索白名单或报告闸门。

### M4-2：本地记录与单轮持久化

状态：已完成（2026-09-15）。记录 Schema、目录布局、原子写、所有权与状态机、
去锁对照与跨进程实测见[开发计划](VendorGuard-Agent开发计划.md) 的 M4-2 完成记录。

修改范围：`review_records.py`、配置、`.env.example`、`.gitignore`、对应测试。

先写测试：UUID 路径不接受外部片段；创建后可逐字段读取；写盘使用原子替换；用户 A 不能
读取用户 B 的记录；同一记录的并发修改不会丢失更新；状态转换非法时拒绝；失败和报告结果
都能保存；材料字节哈希保持不变。

完成判据：不用 FastAPI 也能创建一条本地审查、保存一轮结果、重启进程后重新读取。

### M4-3：受认证的发起、列表和详情接口

修改范围：`review_routes.py`、`app.py`、依赖及 HTTP 测试。

先写测试：未认证 401；有效用户上传文本 PDF 后得到自己的记录；扫描件形成失败记录且模型
未调用；上传超限在模型前拒绝；跨用户统一 404；列表不泄露全文；材料接口字节与上传一致。

同步调用用 `run_in_threadpool()` 包住应用层。测试使用 runtime/store 替身，不访问真实模型；
现有数据库认证测试继续使用原来的回滚 fixture。

完成判据：登录后仅通过 HTTP 即可完成一轮正常审查，并能再次 GET 到相同报告和工具事件。

### M4-4：补充、反馈和重跑接口

修改范围：`review_routes.py`、`review_records.py`、对应测试。

先写测试：只有 question 状态能补充且仅一次；补充后保留两轮记录，第二轮引用来自第二轮
检索；只有完成报告能反馈且不可覆盖；只有可重试失败或要求重查能创建新 ID 和关联，旧失败/
旧反馈不变；扫描件不可重跑；重跑不能携带自选文件路径、模型名或业务状态。

完成判据：HTTP 可走通“追问 -> 补充 -> 报告”和“失败/要求重查 -> 新运行”两条链路。

### M4-5：工作台基础页面

修改范围：`workbench/index.html`、`styles.css`、`app.js`、页面路由和静态资源测试。

实现登录、历史列表、新建审查表单、审查中状态、详情加载和统一错误展示。先用 HTTP 测试确认
资源可访问，再用浏览器检查桌面与 360 px 窄屏；控制台无错误，文字不溢出，键盘可完成表单。

完成判据：用户不用命令行即可登录、上传完整样例并看到报告。

### M4-6：来源、补充、反馈和重跑交互

修改范围：工作台静态资源、必要的响应投影测试。

实现工具轨迹、PDF Blob 预览/页码定位、制度原文展开、一次补充、确认报告、要求重查和创建
关联重跑。所有按钮根据服务端状态显示，前端不自行推导可执行状态。

完成判据：页面能完整演示正常、补充、无依据和失败四条路径；刷新后可从历史记录恢复查看。

### M4-7：真实验收与作品收尾

用真实模型从页面运行：完整材料、缺日期补充、无依据请求各一次；扫描件不发模型请求。
另保留一次真实模型或工具失败并成功创建重跑记录。模型波动按实际结果记录，不挑成功样本
冒充稳定性。

完成桌面与窄屏截图检查、浏览器控制台检查、接口测试、全量单测、Ruff、格式和 mypy。
更新 README、项目上下文、开发计划，并新增一份五分钟演示脚本：从登录到来源核对、失败重跑
和边界说明，所有指标只引用实际验收结果。

## 10. 验收清单

M4 只有同时满足以下条件才算完成：

- CLI 与 HTTP 共用审查应用层，仓库仍只有 `agent.py` 中的一条 Agent 工具循环。
- 已认证用户能上传一份文本 PDF，页面显示完整报告和本轮实际工具事件。
- 补充案例保存两轮结果，第一轮追问不会丢失，第二轮重新校验和检索。
- 制度引用可查看标题、定位和保存的原文；材料来源可定位 PDF 页码或用户补充原文。
- 无依据报告明确显示缺失依据，不把 Top-5 分数写成证据充分度。
- 扫描件、模型/检索失败和预算耗尽显示可理解错误；重跑创建新记录并保留旧失败。
- 报告反馈只记录 `confirmed` / `recheck_requested`，不调用审批接口、不修改案件或 AVL。
- 每个用户只能读取和操作自己的审查记录；跨用户请求统一 404。
- 页面刷新后可重新打开历史报告；本地记录与上传材料不进入 Git。
- 桌面和 360 px 窄屏布局可用，无文字遮挡、横向溢出或不可操作控件；控制台无错误。
- 正常、补充、无依据、失败四条页面路径有真实验收记录，模型波动与人工判断边界如实记录。
- 相关接口测试、全量单测、Ruff、格式检查和 mypy 通过。

## 11. 开始方式

以下为 M4-1 的开工方式，保留备查（该步已于 2026-09-15 完成）。第一步确认 M3 基线：

```powershell
Set-Location D:\projects\VendorGuard
uv run --no-sync pytest -q -p no:cacheprovider tests/unit
```

随后创建 `tests/unit/test_review_application.py` 的第一个失败测试：使用已有模型和检索替身构造
`ReviewRuntime` 与 `ReviewCommand`，断言 `run_review_round()` 返回现有 `AgentRunOutcome`，
且不创建日志文件、不访问数据库。看到测试因 `vendorguard.review_application` 不存在而失败后，
再创建模块并只实现这一条接口。

M4-1 不创建 HTTP 路由、记录目录或页面。应用层接口稳定后，M4-2 再解决跨请求记录问题。
