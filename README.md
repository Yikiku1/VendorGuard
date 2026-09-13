# VendorGuard

供应商材料审查 Agent，面向 AI Agent / AI 应用开发学习与求职演示。

目标体验：提交一份材料，Agent 调用读取、校验和制度检索工具，缺信息时追问，输出带引用的初审报告，由用户确认。

## 当前状态

M1 已完成，M2 待开始。当前可运行的最小 Agent 使用真实模型调用
`check_materials`，并通过显式 `ask_user` 工具追问；工具参数校验、调用预算、
失败出口和脱敏运行记录已经实现。

真实 PDF 输入、制度检索、带引用报告和前端仍待后续里程碑完成。当前入口：

```powershell
uv run --no-sync python -m vendorguard.agent data/demo/agent/normal.json
```

开发顺序和完成标准见 [Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md)，
M1 的设计与验证记录见 [M1 实施方案](project_docs/VendorGuard-M1实施方案.md)。
