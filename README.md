# VendorGuard

供应商材料审查 Agent，面向 AI Agent / AI 应用开发学习与求职演示。

**目标体验：提交一份材料 PDF，Agent 读取原文、提取事实、执行确定性规则，缺信息时追问并在用户补充后继续，检索现行制度依据，输出带来源的初审报告。**

## 当前状态（2026-09-11）

主链路已跑通，可用真实模型演示：

```bash
python -m vendorguard.review data/demo/materials/license_normal.pdf
```

四个样例场景均已用真实模型（`qwen3.7-flash`）实际运行。每次运行会把脱敏记录（模型标识、
工具调用与结果、耗时、token 用量；不含 API Key 与认证头）写入 `logs/agent-runs/`，
该目录不入库：

| 材料 | 期望行为 |
| --- | --- |
| `license_normal.pdf` | 出报告，两条规则 `not_hit`，状态派生 `valid` |
| `license_expired.pdf` | 出报告，`VEN-001` 命中建议补件，状态派生 `expired` |
| `license_missing_date.pdf` | 追问有效期，`--supplement` 补充后同会话继续出报告 |
| `license_scanned.pdf` | 材料层明确拒绝（不支持扫描件），退出码 1 |

前端尚未实现。仍**不访问数据库、不写案件状态、不跑迁移**；产出是初审建议，
不是供应商准入批准。

## 核心设计

模型不可信，所以每个输出都要接回可被程序核验的证据。三道边界都不依赖提示词自觉：

1. **事实不能编造**——每个事实值必须在其声明的来源文本里字面命中，核对失败时规则不执行。
2. **出处不能伪造**——报告引用标签必须来自本次检索的真实返回。
3. **结论不能越权**——模型宣告"准入通过"等措辞被机械拦截并要求改写。

## 已知限制

这些是当前实现的真实边界，不是待办清单：

- **规则未命中不等于准入批准**，产出只是初审建议。
- **证据充分性判定未实现**：检索层只有日期有效性过滤，没有"这段证据是否足以回答这个问题"
  的判断，因此 `no_answer` 类问题仍会召回日期有效但不构成依据的片段。
- **词汇鸿沟**：纯中文二元组 BM25 对同义改写不敏感。评测集 10 道可回答题命中 9/10，
  未命中题为"问 CCC、条文写'列入目录的产品'"。
- **布尔判断只能核对来源**：清单完整性这类判断，程序只能确认来源页码存在，无法验证判断本身正确。
- 仅 `qwen3.7-flash` 实测，未跨模型对比；加密 / 损坏 PDF 未验证。

## 开发

```bash
uv sync                       # 安装依赖
docker compose up -d          # 启动 PostgreSQL（仅集成测试需要）
uv run pytest                 # 345 项测试（276 单元 / 69 集成）
uv run ruff check src tests   # 静态检查
uv run mypy                   # 类型检查（strict）
python scripts/make_demo_materials.py   # 重新生成样例 PDF
```

样例材料为**自制教学材料，非真实证照**，不对应任何真实企业。

文档入口：[当前上下文](PROJECT_CONTEXT.md)、[开发计划](project_docs/VendorGuard-Agent开发计划.md)、
[完整方案](project_docs/VendorGuard-M3M4完整方案.md)、[PRD](project_docs/VendorGuard-PRD.md)。
