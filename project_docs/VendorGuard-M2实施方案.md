# M2：从真实 PDF 进入同一个 Agent 循环

> 日期：2026-09-13。
> 状态：方案已定义，实现未开始。
> 当前唯一小功能：M2-1 文本 PDF 读取。
> 上游依据：[PRD](VendorGuard-PRD.md)、[Agent 开发计划](VendorGuard-Agent开发计划.md)和
> [M1 实施方案](VendorGuard-M1实施方案.md)。

## 1. M2 要交付什么

M2 只把 M1 的结构化模拟输入替换为一种真实材料输入：自制、固定版式、带文本层的 PDF。
完成后的主线是：

```text
用户提交 PDF
    -> 程序登记材料并生成不可伪造的 material_id
    -> Agent 调用 read_material(material_id)
    -> 模型从逐页原文提取两个事实并声明来源页码
    -> 程序核对事实与来源
    -> check_materials 复用现有确定性规则
    -> 信息完整时给出检查说明
    -> 信息缺失时调用 ask_user，用户补充后继续同一会话
```

M2 完成后仍只有一个 Agent 和一个工具循环。`agent.py` 是唯一运行入口；M1 的循环不会保留成
第二套实现。M1 作为已经完成的能力基础继续存在，其预算、错误纠正、工具事件和日志机制由
M2 复用。

## 2. 范围

### 本阶段实现

- 一次提交一份自制文本 PDF。
- 使用 `pypdf` 按页提取文本，保留页码、文件名和 SHA-256。
- 新增业务工具 `read_material`；继续使用 `check_materials` 和控制工具 `ask_user`。
- 模型提取营业执照声明有效期和材料清单是否齐全。
- 程序核对模型声明的来源页码，并由日期派生执照状态。
- 缺字段时追问一次；用户补充后在同一会话继续并重新校验。
- 图片型扫描件明确返回“不支持”，不调用 OCR，也不让模型猜测。
- 运行记录增加材料元数据、事实来源和用户补充轮次，不记录整份材料原文。

### 本阶段不实现

- `search_policy`、向量检索、引用报告和任何 M3 内容。
- `submit_report` 工具；M2 仍沿用 M1 的检查说明出口。
- 多文件、多页复杂版式、表格解析、OCR、图片识别和加密 PDF。
- 数据库存储、案件状态更新、审批、前端和任务恢复。
- LangGraph、多 Agent、工具注册框架或插件系统。

## 3. 三个必须守住的边界

### 3.1 模型不能读取任意路径

命令行先读取用户给定的路径，再由程序生成 `material_id` 并放入当前会话。模型调用
`read_material` 时只能传 `material_id`，不能传文件路径。工具只查当前会话的材料白名单。

### 3.2 模型不能直接声明执照状态

模型提交的是 PDF 中可核对的原始事实：

```json
{
  "business_license_valid_until": "2027-08-31",
  "category_required_documents_complete": true,
  "sources": {
    "business_license_valid_until": "license_complete@page:1",
    "category_required_documents_complete": "license_complete@page:1"
  }
}
```

程序根据参考日期把 `business_license_valid_until` 派生为 `valid` 或 `expired`，再组装既有
`StructuredFacts`。这样模型不能直接选择对规则有利的状态枚举。

### 3.3 来源必须能被程序复核

- 日期：允许 `YYYY-MM-DD`、`YYYY年M月D日` 等固定格式，比较归一化后的年月日。
- 清单：样例材料必须包含明确文字，例如 `材料清单状态: 齐全` 或
  `材料清单状态: 不齐全`。程序按该文字核对布尔值，不从勾选框外观推断。
- 用户补充：程序登记为 `user_supplement@round:1`。不存在的轮次或改造后的内容不能通过。
- 来源不存在、页码越界或事实与原文不一致时，规则不得执行，错误作为工具结果回给模型纠正。

## 4. 模块与接口

本阶段只新增两个模块，避免再次铺开五个并行模块。

| 文件 | 作用 | 对外接口 |
| --- | --- | --- |
| `src/vendorguard/materials.py` | PDF 读取、材料白名单、逐页文本和来源查询 | `read_text_pdf(path) -> MaterialDocument`；`MaterialSources` |
| `src/vendorguard/agent_session.py` | 当前材料、补充内容和轮次 | `ReviewSession.start(material)`；`record_supplement(text)` |

修改现有文件：

| 文件 | 修改内容 |
| --- | --- |
| `src/vendorguard/agent_tools.py` | 增加提取事实 Schema、来源核对和日期派生；继续集中实现 `check_materials` |
| `src/vendorguard/agent.py` | 在现有循环内加入 `read_material` 分发和会话输入；最终由 `run_review(...)` 取代 `run_check(...)`，不保留第二个循环 |
| `pyproject.toml`、`uv.lock` | 增加 `pypdf`；样例生成需要时仅把 `reportlab` 放入开发依赖 |
| `README.md`、`PROJECT_CONTEXT.md`、开发计划 | 只在 M2 验收后更新为“已实现并验证” |

`materials.py` 隐藏 PDF 库细节。Agent 只需要知道材料 ID、页码文本和来源查询，不需要知道
`PdfReader`、图片对象或文件哈希如何计算。测试也通过同一接口验证，不直接依赖内部实现。

## 5. 工具契约

### `read_material`

输入：

```json
{"material_id": "license_complete"}
```

成功输出：

```json
{
  "material_id": "license_complete",
  "page_count": 1,
  "pages": [{"page": 1, "text": "..."}]
}
```

失败情况只有明确错误：材料不属于当前会话、PDF 无法解析、没有文本层或页内容为空。模型不能
通过该工具枚举目录或读取其他文件。

### `check_materials`

模型参数从“执照状态”改为“声明有效期”。处理顺序固定：

1. Pydantic 校验字段、类型和多余参数。
2. 核对每个已提交事实都有来源。
3. 核对来源属于当前材料或已登记的用户补充。
4. 核对日期或清单文字与来源一致。
5. 由程序派生执照状态并构造 `StructuredFacts`。
6. 复用 `evaluate_rule` 执行 `VEN-001`、`VEN-002`。

任一步失败都不执行后续规则。返回继续包含 `evaluations` 和 `missing_fields`，并新增
`verified_sources` 与派生状态，方便日志和面试演示。

### `ask_user`

沿用 M1 的显式追问出口。材料检查中的必需事实缺失时必须先有 `check_materials` 的
`missing_fields` 证据；请求本身不清楚或超出支持范围时，仍保留 M1 已有的澄清出口。问题不能为空，
不能夹带准入结论，也不能暗示补充材料后就能处理不支持的指标。第一轮结束后，调用方把用户回答
登记进 `ReviewSession`，再开始第二轮。M2 只验收一次补充闭环，不扩展长期对话管理。

## 6. 单一循环如何演进

不新建 `review.py`，也不保留两个 CLI。`agent.py` 的现有循环按以下顺序原地演进；M2 完成时
公开运行函数统一命名为 `run_review(...)`，M1 的通用保护测试迁移后删除 `run_check(...)`：

```text
初始状态
  read_done = false
  checked = false

read_material 成功
  read_done = true

check_materials
  若尚未 read -> 拒绝并给一次纠正
  若来源核对失败 -> 拒绝并给一次纠正
  若 missing_fields 非空 -> 只允许 ask_user
  若无缺失 -> checked = true，允许输出检查说明

ask_user
  结束当前轮，返回 kind=question

用户补充
  ReviewSession 登记原文和轮次
  开始下一轮，重新 read/check
```

继续沿用 M1 的限制：每轮最多 8 次模型请求、12 次工具尝试、单请求超时和总时限。迟到响应、
未知工具、多个并行工具、非法参数、空回答和截断回答的保护不能在改造中丢失。

## 7. 样例材料

只准备三个自制样例，不使用真实企业或真实证照：

| 文件 | 内容 | 预期行为 |
| --- | --- | --- |
| `license_complete.pdf` | 有声明有效期和明确的清单状态文字 | read -> check -> 检查说明 |
| `license_missing_date.pdf` | 缺少有效期，清单状态存在 | read -> check -> ask；补充日期后重新 check |
| `license_scanned.pdf` | 只有图片对象，没有文本层 | 程序明确拒绝，不发模型请求 |

若提交生成脚本，脚本只负责复现这三个固定样例，不成为运行时功能。PDF 按二进制纳入 Git，避免
Windows 换行转换破坏文件偏移。

## 8. 实施步骤

每步只提交一个可运行小功能。当前只开始 M2-1，后一步不得提前混入。

### M2-0：冻结基线

目的：证明后续失败由 M2 改动引入。

```powershell
uv run --no-sync pytest -q -p no:cacheprovider tests/unit
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy
```

基线：2026-09-13 已验证 `211 passed`，Ruff、格式和 mypy 通过。实现开始前只需确认工作区没有
混入其他待提交代码。

### M2-1：只实现文本 PDF 读取

修改范围：`materials.py`、`test_materials.py`、`pyproject.toml`、`uv.lock`，以及必要的固定样例。

先写三个聚焦测试：

1. 文本 PDF 返回稳定材料 ID、SHA-256、页数和逐页文本。
2. 文件不存在或内容不是 PDF 时返回可区分错误。
3. 只有图片、没有文本层的 PDF 返回 `scanned_pdf_unsupported`。

本步不改 `agent.py`，不声明工具，不调用模型。完成判据是能从 PowerShell 读取样例并打印第 1 页
文字，聚焦测试和静态检查通过。

### M2-2：只实现事实与来源核对

修改范围：`agent_tools.py` 和聚焦测试。

先写失败用例，证明模型提交错误日期、伪造页码、相反清单状态或不存在的补充轮次时，规则没有
执行。再实现日期归一化、清单文字核对、状态派生和既有规则复用。

本步仍不改 Agent 循环。完成判据是给定 `MaterialSources` 和工具 JSON，可以得到确定性的
`MaterialCheckResult`；错误来源全部明确失败。

### M2-3：把 `read_material` 接入现有循环

修改范围：`agent.py` 与 `test_agent.py`。

先把 M1 中与输入形式无关的保护测试保留下来，再在同一循环增加 `read_material`。钉住调用顺序：

- 未读材料不能 check；
- check 发现缺失后纯文本不能结束；
- 无缺失且 check 成功后才允许检查说明；
- 一次模型响应仍只允许一个工具调用。

完成判据是测试替身走通 `read -> check -> answer`，仓库中仍只有一个循环和一个 CLI。

### M2-4：接受一次用户补充

修改范围：`agent_session.py`、`agent.py` 和对应测试。

先用测试替身走通两轮：第一轮缺日期并返回 `kind=question`；程序登记用户回复；第二轮模型引用
`user_supplement@round:1`，来源核对通过并输出检查说明。补充文字必须原样保留，不能标成 PDF
页来源。

CLI 最终改为接收 PDF 路径。M1 的 JSON 样例入口退出产品主入口，但通用循环保护测试继续保留，
不留下第二套 M1 运行器。

### M2-5：真实模型验收和文档收尾

依次运行完整、缺日期、扫描件三个样例。真实模型只验证工具选择和端到端交互；确定性边界由
单元测试负责。运行记录必须包含模型、材料哈希、工具参数与结果、来源核对、用户补充轮次、耗时
和 token 用量，不能包含 API Key 或整份原文。

全部通过后再把开发计划的 M2 状态改为“已完成”，同步 README 和 PROJECT_CONTEXT。真实模型有
波动时记录实际失败，不挑选一次成功就宣称稳定。

## 9. 验收清单

M2 只有同时满足以下条件才算完成：

- 仓库只有 `agent.py` 一条 Agent 循环和一个 CLI 入口。
- `read_material` 只能读取当前会话已经登记的材料。
- 完整 PDF 能逐页读取，事实来源能回到具体页。
- 日期状态由程序派生，模型提交的虚构日期在规则执行前被拒绝。
- 清单布尔值必须由明确文字支撑，不能只靠页码存在或勾选框外观。
- 缺日期时通过 `ask_user` 追问，补充后在同一会话继续一次。
- 用户补充来源与 PDF 来源可区分，模型不能伪造补充轮次。
- 扫描件在材料层明确失败，不调用模型，不静默返回空文本。
- 不访问数据库，不修改案件状态，不接 RAG、报告和前端。
- 相关单测、全量单测、Ruff、格式检查和 mypy 通过。
- 至少保留一次完整案例和一次补充案例的真实模型运行记录。

## 10. 开始方式

下一次开发只做 M2-1。第一条命令用于确认基线：

```powershell
Set-Location D:\projects\VendorGuard
uv run --no-sync pytest -q -p no:cacheprovider tests/unit
```

随后先创建 `tests/unit/test_materials.py` 的第一个失败测试：读取一页自制文本 PDF 后，断言页数为
1，并且第 1 页包含声明有效期。看到测试因 `vendorguard.materials` 不存在而失败后，再创建
`src/vendorguard/materials.py`，只实现使这一个测试通过的最小代码。

M2 不使用 LangGraph。M3 完成后、M4 开始前再根据是否出现持久化暂停、节点恢复或复杂分支决定
是否做一个小型 LangGraph 对照实验。
