# VendorGuard Claude Code 协作说明

## 每次会话先做

1. 完整阅读 `PROJECT_CONTEXT.md`，它是当前进度和接手点的唯一事实来源。
2. 阅读 `project_docs/VendorGuard-10天计划.md` 中当前 Day；涉及状态迁移时再完整阅读 `project_docs/VendorGuard-状态数据字典.md`。
3. 运行 `git status --short`、`git log -3 --oneline` 和 `uv run alembic current`，确认工作区、提交和数据库迁移状态。
4. 向用户复述当前已完成、未完成和本轮唯一的小功能；得到确认后再推进。

完成标准：当前工作区事实与 `PROJECT_CONTEXT.md` 一致，且没有重复实现已完成能力。

## 与用户协作

- 始终使用中文。用户是第一次独立开发完整项目，需要先理解再动手。
- 业务代码采用“Claude 讲解 -> 用户亲自编写 -> Claude 检查和验证”的顺序。讲解必须包含：做什么、为什么、原理、目标文件、完整写法、预期结果和验证方法。
- 一次推进一个完整且可验证的小功能。不要把一个普通功能拆成逐行确认，也不要跨 Day 提前铺代码。
- 用户提出概念问题时先回答问题；用户未写完当前业务代码前，不代替用户补写。
- Claude 负责进度文档、格式化、集中验收、Git 提交和推送。

## 开发与测试方式

- 状态迁移、审批隔离、追加式审计使用严格 TDD：先确认公共测试边界，再写一个失败测试，观察正确红灯，只实现使它变绿的最小代码。
- 普通模型、CRUD 和数据库查询采用集中实现，并覆盖代表性成功路径与关键数据库约束。
- HTTP 路由只做协议转换；业务规则放在业务模块。RAG 只提供依据，不决定准入或审批。
- `append_audit_event()` 与关联业务变化必须共用事务；审计记录只能追加，数据库负责阻止更新和删除。
- 修改已经应用的未提交迁移前，先把本地数据库降级到它的父迁移；不要让迁移文件与数据库记录失配。

每个小功能完成后运行：

```powershell
uv run pytest
uv run ruff check src tests alembic
uv run ruff format --check src tests alembic
uv run mypy src tests
uv run alembic current
uv run alembic check
git diff --check
```

完成标准：所有命令通过；第三方 `StarletteDeprecationWarning` 是当前已知非阻塞警告。

## 项目边界与安全

- Day 1 和 Day 2 已完成；保留现有配置、日志、数据库、认证核心和 HTTP 认证链路。
- PostgreSQL 容器监听 `127.0.0.1:5433`；Windows 本地 PostgreSQL 使用 `5432`。
- 配置使用 `VENDORGUARD_` 环境变量。不得提交 `.env`、密码、Token、真实供应商数据、日志或本地数据库。
- 保留用户已有修改。Git 工作区非干净时先判断改动归属，不执行破坏性恢复命令。
- 当前范围、已验证结果和下一小步以 `PROJECT_CONTEXT.md` 的“Claude Code 接手点”为准。
