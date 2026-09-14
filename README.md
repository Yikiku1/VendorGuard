# VendorGuard

供应商材料审查 Agent，面向 AI Agent / AI 应用开发学习与求职演示。

目标体验：提交一份材料，Agent 调用读取、校验和制度检索工具，缺信息时追问，输出带引用的初审报告，由用户确认。

## 当前状态

M2 已完成：Agent 从真实的文本 PDF 读取材料，核对模型声明的来源页码，由程序按
参考日期派生执照状态；缺字段时经 `ask_user` 追问，并在同一会话里接受一次用户
补充后重新读取与校验。扫描件在材料层被明确拒绝，不调用 OCR，也不发模型请求。
实现与验证记录见 [M2 实施方案](project_docs/VendorGuard-M2实施方案.md)。

当前入口（样例是自制演示材料，不是真实证照）：

```powershell
uv run --no-sync python -m vendorguard.agent data/demo/materials/license_complete.pdf
```

制度检索、带引用报告和前端仍待后续里程碑完成。

开发顺序和完成标准见 [Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md)，
M1 的设计与验证记录见 [M1 实施方案](project_docs/VendorGuard-M1实施方案.md)。
