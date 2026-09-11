# M2：接入一种真实材料

> 日期：2026-09-11。本文是 M2 实施说明，体例沿用 [M1 实施方案](VendorGuard-M1实施方案.md)。
> 进度状态未开始；当前进度只在 [Agent 开发计划](VendorGuard-Agent开发计划.md) 维护，产品范围见 [PRD](VendorGuard-PRD.md)。
> 本文写在动工之前，其中探测结论已实测（第 5 节），其余为待实施设计。

## 1. 范围与边界

M2 只回答一个问题：**让 Agent 从一份真实 PDF 里读出事实，读不到就问，用户答完接着分析。**

| 做什么 | 不做什么 |
| --- | --- |
| 一种自制、固定版式、可提取文本的 PDF（营业执照） | 扫描件、图片、XLSX、CSV、多种表格 |
| 逐页文本读取，保留页码并支持核对 | 版面还原、表格结构识别、OCR |
| 模型提取两个事实（声明有效期、材料完整性） | 申请表字段一致性核对（`inconsistent` 状态） |
| 缺字段时经 `ask_user` 追问，用户补充后同会话继续 | 跨会话持久化、断点恢复 |
| 日期由程序计算，不采信模型给出的结论枚举 | 通用日期表达式、时区、多日历 |
| 明确提示扫描件不支持 | 静默降级为空文本 |

与 M1 一致的硬约束：M2 仍**不访问数据库、不写案件状态、不调用迁移**，复用规则纯函数。PRD 的三条完成标准（正常 / 缺材料 / 无依据）中，M2 只推进前两条的材料输入环节。

`VEN-003`（质量证书剩余 90 天）仍延期，M2 不启用它；日期计算的落点是 `VEN-001` 的输入。

## 2. 与已有成果的衔接

M1 的模型只回显程序持有的事实，所以"模型不能篡改输入"靠快照对账解决。M2 的事实第一次来自模型读数，没有程序侧真值，边界必须换成另一种形式：

```text
M1：程序持有真值 -> 模型只回显 -> 快照对账挡住改写
M2：模型从材料读数 -> 程序核对"值是否真的出现在所声明的来源里" -> 挡住编造
```

这条"**每个事实值都能在其声明的来源文本里字面找到**"是 M2 的核心机制，替代 M1 的快照对账，而不是取消它。

| 已有文件 | M2 复用内容 |
| --- | --- |
| `policy/facts.py` | `StructuredFacts`、`FactsValidationError`、`certificate_remaining_days()`（首次获得运行时调用方） |
| `policy/evaluator.py` | `evaluate_rule()`，不改动 |
| `policies/rules/v1.0.0.yaml` | `VEN-001` 的 `expired` 分支、`on_missing_input`，以及 `evaluation_contract.date_boundary`（到期日当天仍有效） |
| `agent_tools.py` | `parse_tool_arguments()`、`check_materials()` 的聚合与缺字段逻辑 |
| `agent.py` | 单轮循环、预算、纠正桶、`ask_user`、脱敏运行记录 |
| `data/demo/cases/normal_admission.yaml` | `page:1` 定位格式与 `reference_date: 2026-09-01` 演示基准 |
| `project_docs/VendorGuard-规则测试表.md` | `VEN-001-B1/B2` 到期日边界用例 |

新增依赖只有两项，取舍理由见第 5 节实测：`pypdf` 进运行时依赖，`reportlab` 只进 dev 组（仅用于重新生成样例 PDF）。不引入 `pdfplumber`（更重，自带 pdfminer.six）与 `pymupdf`（AGPL 许可不适合本项目）。

## 3. 样例材料设计

不自造二进制格式，也不手工拼 PDF 语法：用脚本生成固定版式，脚本随仓库提交，样例可复现。

`scripts/make_demo_materials.py` 生成三层结构的营业执照模板 `demo_business_license_v1`（与 `data/demo/cases/*.yaml` 的 `template_id` 一致）：抬头与文档编号、主体信息（统一社会信用代码、企业名称、法定代表人、登记机关）、**声明有效期**、材料清单勾选区。

| 样例 | 内容差异 | 期望结果 |
| --- | --- | --- |
| `data/demo/materials/license_normal.pdf` | 声明有效期 `2027-08-31`，清单齐全 | 日期可核对 → `valid`；两条规则 `not_hit` |
| `data/demo/materials/license_missing_date.pdf` | 整行缺少"声明有效期" | `missing_fields` 含日期 → 必须 `ask_user` |
| `data/demo/materials/license_scanned.pdf` | 页面只有图片对象、无可提取文本 | 解析失败，明确提示不支持扫描件 |

到期日边界（`valid_until` 当天仍有效、次日算过期）用纯函数单测覆盖 `VEN-001-B1/B2`，不为它单独提交第四份 PDF。

样例 PDF 已实测约 3 KB，直接提交；脚本保留使来源可复核，README 需写明"样例为自制教学材料，非真实证照"。

## 4. 工具契约

### 4.1 `read_material`：按 ID 取真实文本

```python
def read_material(
    material_id: str, *, materials: Mapping[str, MaterialText]
) -> MaterialReadResult: ...
```

模型只能按 `material_id` 申请，`materials` 是程序持有的本次提交材料，不由模型提供——与 M1 把 `policy` / `submitted` 放在关键字参数、不进模型参数的写法一致。这同时是 PRD"仅访问用户已提交的材料"的落地方式：白名单之外直接拒绝。

`MaterialReadResult` 含材料 ID、页数、逐页文本、以及 `text_extractable` 标志。**扫描件判定就落在这里**：逐页无可提取文本且页面含图片对象时，返回明确的"不支持扫描件"错误，而不是返回空文本让模型去猜。

### 4.2 提取层与规则层分离

模型提交的事实与规则消费的事实不是同一套，中间由程序做一次确定性换算：

| 层 | 字段 | 谁产出 |
| --- | --- | --- |
| 模型提交层 `ExtractedFacts` | `business_license_declared_valid_until: date \| None`、`category_required_documents_complete: bool \| None`、`sources` | 模型读数 |
| 派生（纯函数） | 日期 + 参考日期 → `valid` / `expired` | 程序 |
| 规则输入层 `StructuredFacts` | `business_license_document_status`、`category_required_documents_complete`、`sources` | 程序组装，**类型与白名单不变** |

这样 `StructuredFacts` 的字段类型与白名单、`evaluate_rule()` 的规则语义、以及全部 `policy` 层测试都不需要改动，规则引擎仍只认那两个既有字段；模型则永远拿不到"自己声明状态枚举"的权力。

PRD 工具表已写明 `check_materials` 的职责是"缺项、**日期计算**与规则结果"，换算因此放在 `check_materials` 内部而非调用方。派生用纯函数，与规则评估同样是可单测的：

```python
def derive_business_license_status(
    *, declared_valid_until: date | None, reference_date: date
) -> BusinessLicenseDocumentStatus | None:
    """由声明有效期派生状态; 缺日期返回 None, 不猜测."""
```

实现复用 `certificate_remaining_days()`：剩余天数 `>= 0` 为 `valid`，`< 0` 为 `expired`，到期日当天仍有效。日期缺失时返回 `None`，进入既有 `missing_fields` 通道，不会被算成某一种状态。M2 只派生这两个值；`inconsistent`（需申请表核对）与 `unreadable`（需可读性判定）明确不在本期范围。

`check_materials` 的**签名需要随迁移更新**：M1 的第二个程序侧依赖是"真值快照"，M2 没有真值可对，改为"可核对的来源文本"。`submitted: StructuredFacts` 因此替换为新增的程序持有对象，`facts` 侧改为提取层：

```python
class SourceTexts(BaseModel):
    """本次可核对的来源: 材料逐页文本与用户补充原文, 均由程序掌握."""

    def contains(self, locator: str, value: object) -> bool: ...


def check_materials(
    facts: ExtractedFacts,
    *,
    policy: PolicyDocument,
    sources: SourceTexts,
    reference_date: date,
) -> MaterialCheckResult: ...
```

`_verify_against_snapshot()` 相应替换为 `_verify_literal_sources()`：不再做相等比对，而是逐字段要求 `sources.contains(locator, value)` 成立。`MaterialCheckResult` 的对外结构（策略版本、规则结果、缺失字段、输入来源）保持不变。

迁移影响面已核对：211 项单测中 `tests/unit/test_agent.py` 与 `tests/unit/test_agent_tools.py` 共 53 项涉及该签名与提取层，需随步骤 B/D 更新；其余 158 项（`policy`、`security`、`logging`、`knowledge`、`retrieval` 等）不受影响。这两个文件已在第 7 节的文件表中列为"修改"。

### 4.3 参考日期必须固定

状态依赖参考日期，用"今天"会让同一份样例隔天得出不同结论，与项目固定的演示基准冲突。参考日期取 PRD 与案例一致的 `2026-09-01`，由 CLI 参数传入、默认固定值，并写入运行记录。这与 RAG 侧的固定演示日期是同一个约定。

### 4.4 字面核对：两种来源，一套机制

每个事实值必须在其声明的来源文本中字面命中，否则拒绝执行：

| 来源定位 | 核对对象 | 能否机械核对 |
| --- | --- | --- |
| `材料ID@page:N` | 该 PDF 第 N 页的提取文本 | 日期可核对：比较数字组（年/月/日），兼容 `2027-08-31` 与 `2027年08月31日` |
| `用户补充@R<轮次>` | 该轮用户补充的原文 | 同上 |

来源定位字符串沿用既有 `文档ID@定位` 格式与 `StructuredFacts` 的来源校验，不改 Schema。合法的材料 ID 与补充轮次由程序掌握，因此模型**无法伪造 `用户补充@R99`**，也无法把用户补充的值标成 `page:1`——PRD"用户文字补充标记为用户提供，不能伪装成 PDF 原文"由此成为结构性约束，而不只是提示词要求。

需要如实记录的不对称：**日期是字面事实，可以机械核对；`category_required_documents_complete` 是模型对清单的判断**，程序只能核对它引用的页码确实存在、并把引用与判断一并存档，不能验证布尔值本身正确。这条限制必须在文档和演示里说明，不得宣称"全部事实已程序验证"。

## 5. 已实测的探测结论

M1 的第 5 节记录过原生 DashScope SDK 报错这一实测事实；M2 同样先探测再设计，两项均已在项目外的临时环境完成（未写入项目依赖）：

1. **中文 PDF 生成与提取可靠。** `reportlab` 的 `UnicodeCIDFont("STSong-Light")` 是内置 CID 字体，**无需随仓库附带 TTF 字体文件**即可渲染中文；`pypdf` 逐页提取中文字符串完全无损、顺序正确，实测样例首行含 `声明有效期至: 2027-08-31`、企业名称与统一社会信用代码均逐字匹配，两页文档页码正确。
2. **扫描件可被确定性识别。** 纯图片页 `extract_text()` 返回空串且页面含 1 个图片 XObject；正常文本页可提取 23 个字符且图片对象数为 0。"无可提取文本 + 存在图片对象"是可用的判据，不需要 OCR 或启发式阈值。

两项结论都只是工具链可行性证据，**不能替代 M2 的验收运行**；真实模型提取的准确性、以及模型是否会按要求省略缺失字段，都必须在实施阶段用真实运行记录验证。

## 6. 会话与失败分类

### 6.1 会话制

M1 的 `run_check()` 每次构造全新 `messages`，追问即结束；M2 需要"补充后在同一会话继续"。改动控制在最小范围：**单轮循环逻辑不动**，新增 `agent_session.py` 承载跨轮状态。

```text
AgentSession: 消息历史 + 已确认事实 + 事实来源 + 轮次计数
每个用户提交（一轮）: 复用 M1 循环 -> 追问(结束本轮) 或 说明(结束会话)
```

这里刻意不叫"快照"：M2 没有程序侧真值快照，会话保存的是模型提取并经核对结果，以及对应的来源。

预算按 PRD"每次用户提交"计，即**每轮重置** `max_model_requests=8` / `max_tool_attempts=12`；会话另加轮次上限（默认 4 轮），防止无界往复。`question_required` 已按最近一次校验结果重算，跨轮天然正确：补充后再校验若无缺失字段，纯文本即可成为结论。用户补充写入历史时标注为用户提供，模型在下一轮才能引用其值。

### 6.2 三类失败必须可区分

开发计划的理解检查要求"能区分解析失败、模型提取错误和业务缺项"。三者走不同通道，互不混淆：

| 失败 | 例子 | 落点 | 用户看到 |
| --- | --- | --- | --- |
| 解析失败 | 扫描件、加密、文件损坏 | `kind=failed`，材料层错误码 | "该文件无可提取文本，暂不支持扫描件" |
| 模型提取错误 | 参数非 JSON、日期格式非法 | 既有纠正桶 `corrections_used` | 纠正后重试；用尽则明确失败 |
| 业务缺项 | 缺声明有效期 | `missing_fields` → `ask_user` | 具体问题，等待补充 |

### 6.3 命令行入口

```bash
python -m vendorguard.agent data/demo/materials/license_missing_date.pdf "请检查材料" \
  --supplement "声明有效期至 2028-12-31" --reference-date 2026-09-01
```

`--supplement` 是主要的可复现路径：补充内容脚本化，运行可重复、可入档。返回追问时若未提供 `--supplement`，则进入交互式标准输入，供手工探索。M1 的事实样例 JSON 入口保留为显式调试参数（记录中仍标 `simulated_input`），使 M1 已归档的四场景可重新复现，且不与"真实解析"混为一谈。

### 6.4 运行记录扩展

沿用 `logs/agent-runs/` 的脱敏 JSON，新增：材料文件名与 **sha256**（使"来源页码能核对"可被第三方复核）、页数、参考日期、每轮用户补充原文、以及每个事实值的来源与核对结果。不新增数据库表。

## 7. 文件安排

按步骤创建，不提前铺骨架。沿用 M1"最小文件"原则，仅在职责确实不同处拆文件：

| 文件 | 改/增 | 职责 |
| --- | --- | --- |
| `src/vendorguard/materials.py` | 新增 | PDF 逐页读取、扫描件判定、来源可核对性检查 |
| `src/vendorguard/agent_session.py` | 新增 | 跨轮状态、轮次上限、补充记录 |
| `src/vendorguard/agent_tools.py` | 修改 | 新增 `read_material`；`check_materials` 接收提取层字段并在内部派生状态 |
| `src/vendorguard/agent.py` | 修改 | 声明 `read_material`、CLI 参数与交互补充；单轮循环保持不变 |
| `src/vendorguard/policy/facts.py` | 修改 | 新增 `derive_business_license_status()` 纯函数 |
| `scripts/make_demo_materials.py` | 新增 | 生成三份自制样例 PDF（可复现） |
| `data/demo/materials/*.pdf` | 新增 | 三份样例材料 |
| `tests/unit/test_materials.py` | 新增 | 逐页提取、扫描件判定、来源核对、数字组兼容 |
| `tests/unit/test_agent_session.py` | 新增 | 会话续接、轮次上限、补充归属 |
| `tests/unit/test_agent_tools.py` | 修改 | 迁移到提取层字段；新增日期派生与字面核对用例 |
| `tests/unit/test_agent.py` | 修改 | 工具声明、追问后继续、失败分类 |
| `tests/unit/test_policy_facts.py` | 修改 | 日期派生的边界用例（当天 / 次日） |

## 8. 分步实施与验收

一次只推进一步，每步跑通再继续。

| 步骤 | 只做这一件事 | 完成判据 |
| --- | --- | --- |
| A 生成样例 | 写生成脚本并产出三份 PDF | 三份文件可由脚本重建；用 `pypdf` 逐页打印可见中文与页码 |
| B 读取工具 | `materials.py` + `read_material` | 正常件逐页文本与页码正确；扫描件返回明确不支持；非白名单 ID 被拒 |
| C 日期派生 | `derive_business_license_status()` 与边界单测 | 参考日期当天为 `valid`、次日为 `expired`；缺日期返回 `None` 而非任一状态 |
| D 接进循环 | 提取层字段 + 字面核对 + 工具声明 | 伪造值（声明页码不含该值）被拒；模型无法把用户补充标成 PDF 来源 |
| E 会话续接 | `agent_session.py` + CLI 补充 | 两份样例真实运行：完整件出说明、缺日期件追问；补充后同会话得出结果 |
| F 记录与复核 | 扩展运行记录，复跑并归档 | 记录含 sha256、页码、逐轮补充；能沿一次记录讲清提取到校验的每一步 |

验收标准（对齐开发计划 M2 完成判据）：

1. 完整与缺字段两份样例**均从实际 PDF 读取**，不从结构化样例搬迁。
2. 来源页码可核对：抽查任一事实值，能在记录的对应页文本中找到。
3. 缺失日期不被编造：缺日期样例必须追问，运行记录中不存在被填入的日期值。
4. 用户补充后同一会话继续分析，且补充值标记为用户提供。
5. 三类失败各有独立、可理解的输出（第 6.2 节）。
6. `pytest tests/unit`、`ruff check src tests`、`ruff format --check src tests`、`mypy` 全部通过。不启动数据库、不跑集成测试（M2 不触碰数据库）。

理解检查：能区分解析失败、模型提取错误与业务缺项，并独立定位其中一个案例；能说明为什么日期必须由程序派生而不是模型给枚举。

## 9. 现在如何开始

第一条命令（步骤 A，不产生模型费用）：

```bash
uv add pypdf && uv add --dev reportlab
uv run python scripts/make_demo_materials.py
```

随后立即用 `pypdf` 打印三份样例的逐页文本，确认中文与页码符合预期，再进入步骤 B。真实模型请求只在步骤 E 才发生，且每次运行记录都落盘，不把一次成功当质量保证。

M2 全程不修改数据库、迁移、`admission`、`audit` 与 `evidence`；如发现需要改动它们才能完成某个案例，先停下来说明是哪个案例无法完成，而不是顺手扩建。
