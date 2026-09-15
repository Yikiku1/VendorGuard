# VendorGuard

供应商材料审查 Agent，面向 AI Agent / AI 应用开发学习与求职演示。

目标体验：提交一份材料，Agent 调用读取、校验和制度检索工具，缺信息时追问，输出带引用的初审报告，由用户确认。

## 当前状态

M3 已完成（制度检索与报告）：Agent 在一个循环里读取文本 PDF 材料、核对来源、
按参考日期派生执照状态，需要制度依据时检索**两份现行内部制度**的 22 条片段，
最后只经 `submit_report` 交付结构化报告——每条结论都要对得上本次校验过的事实与
来源，制度引用只能是本次检索真实返回过的节点，宣告"准入批准"或声称写入状态的措辞
一律拒绝。依据不足时明确说明缺哪类依据，不硬答。运行入口只有一个 CLI。

```powershell
uv run --no-sync python -m vendorguard.agent data/demo/materials/license_complete.pdf
```

样例是自制演示材料，不是真实证照；扫描件在材料层被明确拒绝，不调用 OCR，也不发模型请求。

限定与非目标（首版有意如此，不是待办）：

- 候选集由代码白名单固定为 `demo_supplier_admission_policy_v2` 与
  `demo_supplier_required_documents_policy_v3`；旧版本与外部法规留在仓库作负例，
  不进入检索，也不做按日期回放历史版本。
- 本地 embedding 缓存 + 精确 cosine 单路检索，返回 Top-5；不设分数阈值，分数不参与
  判定，也不交给模型。
- 不接数据库、不接前端、不做多 Agent；`evidence/models.py` 里的持久化实体保持暂停。
- 评测只用 PRD 选定的 8 题，其余 7 题（历史日期与外部法规）明确未覆盖。

已入库的检索数据与脚本：

```powershell
uv run --no-sync python scripts/build_chunk_list.py      # 生成 22 条片段清单
uv run --no-sync python scripts/build_embedding_cache.py # 生成/复用正文向量缓存
uv run --no-sync python scripts/preview_search.py "查询"  # 命令行看 Top-5
uv run --no-sync python scripts/run_rag_eval.py          # 8 题评测并写出结果
```

实现与验证记录见 [M3 实施方案](project_docs/VendorGuard-M3实施方案.md)，
开发顺序和完成标准见 [Agent 开发计划](project_docs/VendorGuard-Agent开发计划.md)，
M1、M2 的设计与验证记录见各自的实施方案。
