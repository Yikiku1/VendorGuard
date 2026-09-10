# M1：第一个可以解释的工具调用 Agent

> 日期：2026-09-08。本文是 M1 实施说明。
> 进度：步骤 A-E 于 2026-09-10 完成并通过两轮外部评审修正闭环，M1 收官（超范围追问场景记为已知限制，见开发计划 M1 节）；下一里程碑见 [开发计划](VendorGuard-Agent开发计划.md)。
> 当前进度只在 [开发计划](VendorGuard-Agent开发计划.md) 维护；产品范围见 [PRD](VendorGuard-PRD.md)。

## 1. 文件清理决定

2026-09-08 已按用户要求完成下表的占位清理，共删除 9 个文件，并移除旧工具安装提示。已有业务实现、RAG 成果、数据库和用户改动保留，M1 可以直接开始。

| 对象 | 决定 | 已核对的原因 |
| --- | --- | --- |
| `src/vendorguard/approvals/__init__.py` | 已删除 | 只有模块说明，无实现、无代码引用；真实决定逻辑在 `admission` |
| `src/vendorguard/purchase_exceptions/__init__.py` | 已删除 | 只有模块说明，无实现、无代码引用；采购例外已退出范围 |
| `policies/admission/.gitkeep`、`policies/approval/.gitkeep` | 已删除 | 空目录占位，无代码引用；实际规则在 `policies/rules/` |
| `data/demo/cases/.gitkeep`、`data/evals/.gitkeep`、`data/knowledge/policies/.gitkeep`、`tests/unit/.gitkeep`、`tests/integration/.gitkeep` | 已删除 | 所在目录已有实际文件，已无保留空目录作用 |
| `AGENTS.md` 的 Compound Pi 安装提示块 | 已移除 | 当前开发方式不依赖该工具映射；机器上的工具未卸载 |
| `admission`、认证、审计、数据库及现有迁移和测试 | 保留，M1 不接入 | 已有工作相互引用；删除会引入回归与迁移成本 |
| `evidence/models.py`、迁移 `b0604f36e9a4` | 保留，暂停扩展 | 模型由 `alembic/env.py` 导入，且存在用户未提交改动；本轮未查数据库实际版本 |
| 知识库旧 YAML、快照、归一化正文和原评测集 | 保留 | 部分被测试读取，历史版本也是负例；M3 再按使用范围取子集 |
| 旧案例 YAML、历史文档及十天计划跳转入口 | 保留 | 保留测试与历史来源；M1 明确只复用正常案例的事实部分 |
| `frontend/.gitkeep`、尚空的测试目录 | 暂留 | 不增加运行复杂度；对应页面或测试阶段到来时再替换 |
| `.idea/`、`.env`、`.venv/`、锁文件、缓存 | 本轮不删除 | 属于用户环境或依赖复现内容；不以清理项目为由重建开发环境 |

上述清理使用逐文件补丁；涉及实际业务模块的删除仍需另行核对引用和数据库状态。当前无需移除任何 Python 依赖。

## 2. 与之前进度的衔接

旧系统用规则结果驱动案件状态。M1 取出同一套规则纯函数，作为模型可调用的工具：

```text
原实现：结构化事实 -> evaluate_admission_case -> 规则结果 + 案件状态 + 审计
M1：   结构化事实 -> Agent -> check_materials -> evaluate_rule
                                工具结果 -> Agent -> 解释或追问
```

| 已有文件 | M1 复用内容 |
| --- | --- |
| `policy/facts.py` | `StructuredFacts`、`FactsValidationError`、正常案例事实加载器 |
| `policy/schema.py` | `load_policy()`，读取并校验现有规则配置 |
| `policy/evaluator.py` | `evaluate_rule()`，只执行启用规则 |
| `policies/rules/v1.0.0.yaml` | `VEN-001`、`VEN-002` 的条件、结果与缺输入语义 |
| `config.py` | 现有 `VENDORGUARD_` 配置风格，模型配置到接入阶段再增加 |

日期计算函数留给 M2 的真实日期输入。M1 不额外增加日期工具，也不修改旧规则 Schema 中的 `scoped_for_10_day_mvp` 兼容值。

本轮已运行三个规则相关单测文件，22 项通过。另用正常案例执行两条规则，结果均为 `not_hit`，且没有导入 `vendorguard.database` 或 `vendorguard.admission`。不启动 Docker、FastAPI、不运行迁移即可开始 M1。

旧 `supplement_review.yaml` 不能直接作为 M1 补件示例：实测 `initial_submission` 加载后的两项事实均为 `None`，`after_supplement` 的材料完整性仍为 `None`。原因是该字段写在阶段顶层，而加载器只取 `structured_facts` 内的白名单字段；现有测试也明确保留了后一行为。M1 用正常事实生成明确的测试变体，不改变旧案例加载契约。

## 3. 最小文件安排

按步骤创建，不提前铺骨架。M1 最多新增两个业务文件、两个测试文件：

| 文件 | 职责 |
| --- | --- |
| `src/vendorguard/agent_tools.py` | `check_materials` 的输入输出、事实一致性检查、纯函数规则适配 |
| `src/vendorguard/agent.py` | Prompt、单供应商 SDK 接入、有限循环、命令行入口和本地运行记录 |
| `tests/unit/test_agent_tools.py` | 校验工具的实际行为与事实边界 |
| `tests/unit/test_agent.py` | 用模型响应替身验证分支、预算与错误处理 |

模型接入时才修改 `config.py`、`.env.example`、`pyproject.toml` 和锁文件。先沿用单文件组织，只有明显难读才拆分；M1 不建立 provider 工厂、工具插件系统、repository 或新数据库表。

## 4. 工具契约

模型调用 `check_materials`，提交两项事实及来源；运行器另持有用户实际提交的事实快照，不能让模型覆盖它。Python 接口可采用：

```python
def check_materials(
    facts: StructuredFacts, *, policy: PolicyDocument
) -> MaterialCheckResult:
    ...
```

`policy` 是程序加载的依赖，不进入模型参数。结果模型也放在 `agent_tools.py`：包含策略版本、已检查的规则 ID、每条规则结果、缺失字段和输入来源。输出复用 `RuleEvaluation`，不要重复创建另一套规则语义。

执行过程：

1. 用结构化 JSON parser 读取参数，再用 `StructuredFacts` 验证；捕获 `FactsValidationError`，不能只捕获 Pydantic 的 `ValidationError`。
2. 对比模型参数与本次真实输入快照的字段和来源。模型补造、改写或漏传已有事实时返回参数错误，不能把假参数交给规则执行。M1 校验来源与模拟输入一致，不声称验证了真实 PDF。
3. 用 `facts.model_dump(exclude={"sources"}, exclude_none=True)` 生成规则输入。`None` 必须移除：执行器通过键是否存在识别缺输入，传入 `None` 可能得到错误的 `not_hit`。
4. 只遍历 `implementation_scope.enabled_rule_ids` 对应规则，以 `supplier_admission` 执行。
5. 原样返回规则结果；`target_case_status` 只是旧规则数据，不调用任何状态迁移函数。

`false` 表示已知材料不齐，`None` 表示完整性未知，两者必须分开。已知不齐时不能仅凭布尔值猜出具体缺哪份文件；没有具体清单就询问清单。两条规则均未命中也只表示本次检查未发现这些问题，不等于准入批准。

## 5. Agent 执行流程

### 默认模型与接入方式

2026-09-08 根据用户“推荐国产模型”的要求，推荐阿里云百炼官方服务的 `qwen3.7-flash`，M1 使用非思考模式，优先通过 DashScope Python SDK 直连。协议与具体 API 方法按该模型的官方示例核对，不自行猜测纯文本或多模态 SDK 入口。

官方 Function Calling 文档列出 Qwen3.7-Flash 系列，价格页当前将该别名对应到 `qwen3.7-flash-2026-07-15`。首次跑通后，若当前账号支持该快照 ID，固定快照用于演示复现；记录实际模型 ID，不把滚动别名当作永远不变的版本。

北京地域、单次输入不超过 32K Token 时，官方标价为输入 0.2 元/百万 Token、输出 0.8 元/百万 Token。假设一次 Agent 运行各轮累计输入 10,000、输出 2,000 Token，且每次请求都在该档，模型费用约为 0.0036 元，100 次约 0.36 元。这是算例，不是实测运行成本；每轮重发历史也计入输入，其他地域和长上下文价格不同。

M1 的一个工具与两项事实不需要旗舰推理能力。先验证参数合法、工具结果利用正确、缺信息会追问；模型出现稳定可复现的失败时，先检查工具定义和消息往返，再用更强模型对照定位原因。

上述选型只核对了官方资料，尚未调用用户账号或验证模型可用性。接入前在百炼控制台选择地域、获取 API Key 与当前业务空间端点；Key 只写本地 `.env`。地域和端点必须匹配，按控制台配置，不在本文硬编码公共网关。

官方依据（2026-09-08 查询）：[Function Calling](https://help.aliyun.com/zh/model-studio/qwen-function-calling)、[模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[获取 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)。

### 调用循环

```text
加载用户事实与规则 -> 将用户请求、事实和一个工具定义交给模型
  -> 返回工具调用：校验名称与参数 -> 执行 -> 按调用 ID 回传结果 -> 再次请求模型
  -> 返回追问：显示问题，结束本次运行
  -> 返回检查说明：核对已有工具执行结果，显示说明，结束本次运行
  -> 超时、超预算或无法纠正的错误：明确失败，保留记录
```

采用选定供应商的官方 SDK，具体消息字段由实际协议决定。保留供应商协议要求回传的调用信息，不能丢失工具调用 ID 或把工具结果伪装成用户消息。不另造一层跨供应商消息框架。

正常模式允许模型自动选择是否调用工具；若没有成功校验就声称已检查，运行器要求纠正，最多一次且计入预算。允许信息不足时直接追问，不能靠 Prompt 假设每次一定调用正确。

参数错误和未知工具均不执行，以结构化错误回传并允许一次纠正；该纠正次数在一次用户提交中共享。网络失败或超时直接结束并支持手动重新运行，SDK 自动重试设为 0，防止实际调用次数超过预算。

每次用户提交最多 8 次模型请求、12 次工具调用尝试（非法调用也计数），单次模型超时初值 30 秒、整次运行总时限 120 秒。每次等待限制在剩余总时限内。模型一次返回多个调用时也逐个检查预算，达到上限即终止；所有上限均为可配置的教学默认值。

M1 的输出只是“检查说明或追问”，不实现 M3 的带制度引用报告。不检查模型隐藏思维过程，也不把模型口头解释当作真实工具执行记录。程序展示原始校验结果，模型说明放在旁边；语义是否忠实通过固定演示案例检查，不声称 Schema 能验证全部自然语言。

追问在 M1 是本次运行的终点；学习时修改结构化输入并重新运行验证。M2 再实现同会话接受自然语言补充、合并事实与继续调用。

## 6. 分步实施与验收

| 步骤 | 只做这一件事 | 完成判据 | 状态 |
| --- | --- | --- | --- |
| A 复用旧规则 | 运行下一节命令，读懂输入和结果 | 能解释两条 `not_hit` 和 `exclude_none=True` | 已完成：2026-09-08，22 项测试与探针通过 |
| B 包装工具 | 写 `agent_tools.py` 与聚焦测试 | 正常、不齐、未知、过期有正确结果；伪造输入拒绝 | 已完成：2026-09-08，`test_agent_tools.py` 11 项通过 |
| C 接通模型 | 确认服务、模型名和 SDK，做一次受控工具往返 | 真实模型产生参数，程序执行，模型消费返回值；不是仅聊天成功 | 已完成：2026-09-08，OpenAI 兼容协议两轮往返跑通，回答忠实（明确 not_hit 不等于批准） |
| D 有限循环 | 从一次往返扩展到条件循环和追问 | 替身测试覆盖未知工具、坏 JSON、参数错误、无调用假结论、超时和预算上限 | 已完成：2026-09-09，`agent.py` + 11 项替身测试，181 单测/ruff/mypy 全绿 |
| E 演示与理解 | 正常、缺项、未知三个真实模型案例 | 记录实际结果，能解释失败并亲手修改一处行为 | 已完成：2026-09-10；首轮“未知”案例经评审判定不忠实并撤回结论，修正（工具背书、忠实性提示词、记录补全）后重跑通过，追问另在超范围场景真实实证；E3 实验后按规范回滚 |

C 的协议探针可以强制调用唯一工具以确认兼容性，但不能把这个结果当作 E 的 Agent 自主选择验收。E 使用正常自动工具选择模式。所有付费模型请求均在开始接入实施时才执行。

建议的最小测试输入由测试函数在正常事实基础上构造：正常 `valid/true`；不齐 `valid/false`；未知时移除完整性字段及其来源；过期 `expired/true`。变更事实只发生在测试输入或用户输入准备阶段，不允许模型自行修改后通过一致性校验。

B 运行工具测试和现有三个规则测试；D 增加循环测试与所改文件的 lint、类型检查；E 运行实际模型案例。无需为 M1 修复所有旧数据库集成测试或启动数据库。

本地记录使用已忽略的 `logs/agent-runs/`，一个运行一个 JSON：模型标识、模拟输入标记、脱敏后的实际工具调用及结果、最终说明或错误、耗时、可获得的 token 用量。不记录 API Key、请求认证头或隐藏推理。

## 7. 现在如何开始

第一步不需要安装新依赖。PowerShell 从仓库根目录运行：

```powershell
Set-Location D:\projects\VendorGuard
uv run --no-sync pytest -q -p no:cacheprovider tests/unit/test_policy_schema.py tests/unit/test_policy_facts.py tests/unit/test_policy_evaluator.py
```

本轮实测是 22 项通过；`-p no:cacheprovider` 避免当前 `.pytest_cache` 写权限提示，不修改缓存权限。`--no-sync` 使用现有环境；新机器需先按锁文件恢复依赖。

然后执行这个只读小实验，亲眼看到旧代码怎样接入工具：

```powershell
$probe = @'
from pathlib import Path
from vendorguard.policy import load_demo_case_facts, load_policy, evaluate_rule

facts = load_demo_case_facts(Path("data/demo/cases/normal_admission.yaml"))
policy = load_policy(Path("policies/rules/v1.0.0.yaml"))
values = facts.model_dump(exclude={"sources"}, exclude_none=True)

for rule in policy.rules:
    if rule.id in policy.implementation_scope.enabled_rule_ids:
        result = evaluate_rule(rule, scope="supplier_admission", facts=values)
        print(f"{result.rule_id}: {result.result}")
'@
uv run --no-sync python -c $probe
```

预期：`VEN-001: not_hit`、`VEN-002: not_hit`。下一次只把这段业务逻辑包装成 `check_materials()` 并验证几个输入，不同时开始模型、PDF、RAG 和页面。

C 按上述百炼官方服务、`qwen3.7-flash`、OpenAI 兼容协议与 `openai` SDK 方案开始（实测记录见第 5 节）；不要在聊天里发送 API Key。模型配置已落地 `.env`：`VENDORGUARD_LLM_MODEL`、`VENDORGUARD_LLM_API_KEY` 与 `VENDORGUARD_LLM_BASE_URL`（模板见 `.env.example`），只在 Agent 启动入口检查必需项，旧后端不因缺少模型配置而不能启动。

正式启动命令：`uv run --no-sync python -m vendorguard.agent <事实样例> [请求]`，样例在 `data/demo/agent/`。运行记录（含密钥打码）写入 `logs/agent-runs/`。M2 接入 `read_material`，M3 接入 `search_policy`，M4 把同一个运行函数放到页面后端使用，不重新复制一个 Agent。追问为交互控制工具 `ask_user`，不计入 M1-M3 业务工具总数。
