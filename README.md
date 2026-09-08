# VendorGuard

供应商材料审查 Agent，面向 AI Agent / AI 应用开发学习与求职演示。

目标体验：提交一份材料，Agent 调用读取、校验和制度检索工具，缺信息时追问，输出带引用的初审报告，由用户确认。

当前已有 FastAPI 后端、认证、案件接口、两条确定性规则、制度语料及节点解析代码。Agent 工具循环、真实 PDF 输入、检索链路和前端尚未完成；当前仓库不能演示上述完整体验。

- [当前状态](PROJECT_CONTEXT.md)
- [产品范围](project_docs/VendorGuard-PRD.md)
- [开发计划](project_docs/VendorGuard-Agent开发计划.md)

2026-09-08 起停止原十天计划，优先完成单 Agent 演示。已有业务代码和数据库保留，暂不扩建企业审批平台。
