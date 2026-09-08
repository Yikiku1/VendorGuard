# VendorGuard 规则测试表

> 文档类型：Day 1 规则输入与边界基线  
> 文档状态：领域规则已确认，实施范围已精简  
> 确认日期：2026-08-27  
> 适用范围：`VEN-001` 至 `VEN-006`、`PR-001`  
> 说明：本表保留历史测试设计，不代表当前实现状态；当前事实见 [项目上下文](../PROJECT_CONTEXT.md)。

## 本期实施范围

2026-09-08 起，仅复用现有 `VEN-001`、`VEN-002` 作为 Agent 校验工具的基础，启用配置见 `policies/rules/v1.0.0.yaml`。七条规则及 43 个边界案例保留为历史设计，其余规则不进入当前交付要求；范围见 [PRD](VendorGuard-PRD.md)。

## 1. 为什么先写测试表

同一句“证书不足 90 天”如果不先固定边界，后续可能出现三种互相冲突的实现：小于 90、小于等于 90，或者先四舍五入再比较。测试表先把字段、运算符、前置条件和临界值写清楚，后续规则 YAML、Python 规则引擎和 pytest 才能使用同一套事实。

规则只产生风险命中、补件要求或审批要求，不直接批准供应商或采购例外。最终决定仍由有权限的人工审批人作出。

## 2. 通用约定

| 项目 | 约定 |
| --- | --- |
| 阈值性质 | 90 天、20% 和 90% 均为教学模拟值，后续必须写入版本化配置 |
| 时间基准 | 每次评估保存 `evaluated_at`；日期类材料按该时间对应的业务日期计算 |
| 到期日 | 建议有效期包含 `valid_until` 当天；只有评估日期晚于该日期才算过期 |
| 百分比计算 | 使用未四舍五入的十进制定点数比较阈值，页面展示时才保留两位小数 |
| 规则结果 | `hit` 表示适用且条件成立；`not_hit` 表示适用且条件不成立；`not_applicable` 表示规则不属于当前案件类型 |
| 前置条件不足 | 不把“无法比较”伪装成 `not_hit`；不执行对应阈值比较，另建数据质量或人工复核项 |
| 规则历史 | 重新评估产生新结果，不覆盖旧的规则命中、材料来源或审批意见 |
| 审批合并 | 多条规则要求相同角色时按角色去重；不同角色分别创建审批任务 |
| 人工拒绝 | 任一必需审批角色拒绝后，案件不能被其他批准覆盖 |
| 数据边界 | 只核验项目内材料和声明字段，不声称完成工商、认证机构或 ERP 权威验证 |

这里的规则结果与“证据充分性”是不同概念。证据尚未评估时仍使用空值，不为证据充分性增加 `not_evaluated`。

## 3. 规则输入字段字典

| 字段 | 类型 | 允许值或单位 | 来源与计算 | 使用规则 |
| --- | --- | --- | --- | --- |
| `evaluated_at` | 带时区时间 | ISO 8601 | 每次规则评估的固定时钟 | 所有日期和有效期判断 |
| `business_license_document_status` | 枚举 | `valid`、`expired`、`inconsistent`、`unreadable` | 营业执照声明日期、可读性及其与申请表字段的一致性 | `VEN-001` |
| `category_required_documents_complete` | 布尔 | `true`、`false` | 品类必填材料集合是否为已提供材料集合的子集 | `VEN-002` |
| `quality_certificate_remaining_days` | 整数 | 天，可为负数 | `declared_valid_until - evaluation_date` | `VEN-003` |
| `quote_is_comparable` | 布尔 | `true`、`false` | 币种、单位、税口径和基准日期均可比且基准价大于 0 | `VEN-004` 前置条件 |
| `quote_deviation_percent` | 十进制定点数 | 百分比 | `(quoted_unit_price - baseline_unit_price) / baseline_unit_price * 100` | `VEN-004` |
| `delivery_history_is_sufficient` | 布尔 | `true`、`false` | 是否达到后续解析阶段配置的最小有效履约样本要求 | `VEN-005` 前置条件 |
| `on_time_delivery_rate` | 十进制定点数 | `0` 至 `100` | `on_time_deliveries / valid_delivery_samples * 100` | `VEN-005` |
| `category` | 枚举代码 | `critical_material` 或其他已配置品类 | 申请或 PR 的物料品类 | `VEN-006` |
| `supplier_is_in_avl` | 布尔 | `true`、`false` | 由资格状态、资格有效期和生效中的暂停决定共同生成的 AVL 查询投影 | `PR-001` 的直接输入 |
| `supplier_eligibility_status` | 枚举 | `candidate`、`approved`、`suspended`、`expired`、`rejected` | 创建采购例外时保存的资格快照 | `PR-001` 的审计解释，不单独作为放行判断 |

`delivery_history_is_sufficient` 的最小样本数量将在 Day 5 解析与计算设计中配置。本规则只消费已经计算好的布尔事实，当前不提前写死样本数量。

## 4. 规则摘要

| ID | 适用流程 | 条件 | 动作 | 审批角色 |
| --- | --- | --- | --- | --- |
| `VEN-001` | 供应商准入 | `business_license_document_status in [expired, inconsistent, unreadable]` | 阻断或转人工核验 | 视异常类型处理 |
| `VEN-002` | 供应商准入 | `category_required_documents_complete == false` | 要求补件 | 无 |
| `VEN-003` | 供应商准入 | `quality_certificate_remaining_days < 90` | 创建质量复核 | `quality_manager` |
| `VEN-004` | 供应商准入 | `quote_is_comparable == true and quote_deviation_percent > 20` | 创建采购复核 | `procurement_manager` |
| `VEN-005` | 供应商准入 | `delivery_history_is_sufficient == true and on_time_delivery_rate < 90` | 创建采购复核 | `procurement_manager` |
| `VEN-006` | 供应商准入、关键物料采购例外 | `category == critical_material` | 创建双审批 | `procurement_manager`、`quality_manager` |
| `PR-001` | 采购例外 | `supplier_is_in_avl == false` | 创建仅绑定当前 PR 的例外审批 | `procurement_manager`；关键物料再合并 `VEN-006` |

## 5. `VEN-001` 营业执照材料异常

### 5.1 处置建议

- `expired`：阻止进入后续分析并要求提供当前有效材料。
- `inconsistent`：转人工核验，可由审批人要求补件。
- `unreadable`：转人工核验并要求重新上传可读材料。
- `valid`：本规则不命中。

### 5.2 测试样例

| 用例 | 输入 | 预期 | 说明 |
| --- | --- | --- | --- |
| `VEN-001-H1` | `expired` | 命中，阻断并补充当前有效材料 | 声明有效期已结束 |
| `VEN-001-H2` | `inconsistent` | 命中，人工核验 | 营业执照与申请表字段矛盾 |
| `VEN-001-H3` | `unreadable` | 命中，人工核验并重传 | 文本不可判读，不等于材料缺失 |
| `VEN-001-N1` | `valid` | 不命中 | 仅代表上传材料内部检查通过 |
| `VEN-001-B1` | `evaluation_date == declared_valid_until` | 视为 `valid`，不命中 | 建议有效期包含到期当天 |
| `VEN-001-B2` | `evaluation_date == declared_valid_until + 1 day` | 视为 `expired`，命中 | 第一个过期日 |

## 6. `VEN-002` 品类必填材料缺失

| 用例 | 输入 | 预期 | 说明 |
| --- | --- | --- | --- |
| `VEN-002-H1` | 必填质量证书缺失 | 命中，进入 `pending_documents` | 补件案例首次提交 |
| `VEN-002-N1` | 所有必填材料齐全 | 不命中 | 正常准入或补件完成后 |
| `VEN-002-B1` | 只缺可选履约记录 | 不命中 | 可选材料不影响完整性布尔值 |
| `VEN-002-B2` | 同一必填文件重复两份，但另一必填类型缺失 | 命中 | 重复文件不能替代缺失类型 |
| `VEN-002-B3` | 品类不要求质量证书且未上传 | 不命中 | 必填集合必须按品类配置 |

## 7. `VEN-003` 质量证书剩余不足 90 天

| 用例 | 剩余天数 | 预期 | 说明 |
| --- | ---: | --- | --- |
| `VEN-003-H1` | `60` | 命中，质量经理复核 | 补件案例 |
| `VEN-003-B1` | `89` | 命中，质量经理复核 | 小于 90 |
| `VEN-003-B2` | `90` | 不命中 | 等于阈值不命中 |
| `VEN-003-N1` | `91` | 不命中 | 大于阈值 |
| `VEN-003-E1` | `-1` | 命中，质量经理复核 | 一期不新增规则；负数表示材料已过期，真实企业可改为阻断策略 |
| `VEN-003-P1` | 质量证书缺失 | 不执行本规则 | 先由 `VEN-002` 补件，不能把缺失当作临期 |

## 8. `VEN-004` 可比报价高于基准价 20%

| 用例 | 可比 | 偏离率 | 预期 | 说明 |
| --- | --- | ---: | --- | --- |
| `VEN-004-H1` | `true` | `25.00` | 命中，采购经理复核 | 补件案例 |
| `VEN-004-B1` | `true` | `20.00` | 不命中 | 条件是大于 20，不包含等于 20 |
| `VEN-004-B2` | `true` | `20.01` | 命中，采购经理复核 | 第一个两位小数展示边界，仅用于阅读 |
| `VEN-004-N1` | `true` | `8.00` | 不命中 | 正常准入案例 |
| `VEN-004-P1` | `false` | 任意值 | 不执行偏离阈值比较，创建口径不足复核项 | 币种、单位、税口径或基准日期不可比 |
| `VEN-004-P2` | `false` | 无 | 不执行偏离阈值比较，创建数据质量复核项 | 基准价为 0，禁止除零 |

实现时必须使用原始十进制定点值比较，不先把 `20.004` 四舍五入为 `20.00` 后再判断。

## 9. `VEN-005` 准时交付率低于 90%

| 用例 | 样本充分 | 准时交付率 | 预期 | 说明 |
| --- | --- | ---: | --- | --- |
| `VEN-005-H1` | `true` | `89.99` | 命中，采购经理复核 | 小于 90 |
| `VEN-005-B1` | `true` | `90.00` | 不命中 | 等于阈值不命中 |
| `VEN-005-N1` | `true` | `96.00` | 不命中 | 正常准入案例 |
| `VEN-005-P1` | `false` | 任意值 | 不执行履约率阈值比较，生成“历史数据不足”复核项 | 样本数量不足 |
| `VEN-005-P2` | `false` | 无 | 不执行履约率阈值比较，生成“历史数据不足”复核项 | 首次合作且无历史数据 |

历史不足不是低履约率，也不是技术运行失败。

## 10. `VEN-006` 关键物料双审批

| 用例 | 品类 | 预期 | 说明 |
| --- | --- | --- | --- |
| `VEN-006-H1` | `critical_material` | 命中，创建采购经理和质量经理两个任务 | 任一人不能代表另一角色 |
| `VEN-006-N1` | `standard_fasteners` | 不命中 | 普通物料不触发本规则 |
| `VEN-006-B1` | 关键物料采购例外同时命中 `PR-001` | 两条规则的角色取并集并去重，最终仍为两个任务 | 采购经理任务不能重复创建 |

## 11. `PR-001` 非 AVL 供应商采购例外

### 11.1 已确认的规则输入

PRD 原示例只比较供应商资格枚举。但 AVL 还要求资格未到期且不存在生效中的暂停决定，因此仅判断资格枚举不够稳妥。正式规则确认为：

```text
supplier_is_in_avl == false
```

创建采购例外时仍保存 `supplier_eligibility_status`、资格有效期和暂停信息快照，用于审计解释。

### 11.2 测试样例

| 用例 | AVL 投影 | 资格快照 | 预期 | 说明 |
| --- | --- | --- | --- | --- |
| `PR-001-H1` | `false` | `candidate` | 命中，创建绑定当前 PR 的例外审批 | 采购例外案例 |
| `PR-001-N1` | `true` | `approved` 且有效、未暂停 | 不命中，PR 可走常规采购控制 | 仍不代表 VendorGuard 创建 PO |
| `PR-001-B1` | `false` | `approved` 但资格有效期已结束 | 命中 | 防止只看枚举造成错误放行 |
| `PR-001-B2` | `false` | `suspended` | 命中 | 暂停期间不能进入 AVL |
| `PR-001-B3` | `false` | `expired` 或 `rejected` | 命中 | 必须走单次例外或更换供应商 |
| `PR-001-S1` | 例外已批准 | 供应商仍非 AVL | 只放行绑定 PR，其他 PR 仍命中 | 例外批准不改变供应商资格 |
| `PR-001-S2` | 例外已过期 | 供应商仍非 AVL | 原 PR 不得继续，重新申请 | 过期例外不能恢复或复用 |

## 12. 审批要求合并测试

| 用例 | 规则命中 | 预期审批任务 |
| --- | --- | --- |
| `APPROVAL-MERGE-01` | 仅 `VEN-004` | 一个 `procurement_manager` 任务 |
| `APPROVAL-MERGE-02` | `VEN-003`、`VEN-004` | 一个 `quality_manager`、一个 `procurement_manager` 任务 |
| `APPROVAL-MERGE-03` | `VEN-006`、`PR-001` | 一个 `quality_manager`、一个 `procurement_manager` 任务，采购经理不重复 |
| `APPROVAL-MERGE-04` | 任一必需角色拒绝 | 案件不能批准，其他批准不能覆盖拒绝 |
| `APPROVAL-MERGE-05` | 申请人同时拥有所需角色 | 仍不能审批自己提交的案件 |

## 13. 三个演示案例对照

| 案例 | 当前预期命中 | 必须保留的历史命中 | 审批角色 | 最终业务结果 |
| --- | --- | --- | --- | --- |
| [正常准入](../data/demo/cases/normal_admission.yaml) | 无 | 无 | 采购经理最终确认 | 准入案件和供应商资格均为 `approved`，进入 AVL |
| [补件后复核](../data/demo/cases/supplement_review.yaml) | `VEN-003`、`VEN-004` | `VEN-002`、`VEN-003`、`VEN-004` | 质量经理、采购经理 | 限时准入，原规则命中不删除 |
| [采购例外](../data/demo/cases/procurement_exception.yaml) | `VEN-006`、`PR-001` | 同当前命中 | 质量经理、采购经理 | 仅指定 PR 在范围和有效期内继续，供应商仍非 AVL |

## 14. 已确认的四项建模选择

1. 营业执照和资格有效期包含 `valid_until` 当天，下一天才算过期。
2. `PR-001` 使用 `supplier_is_in_avl == false`，资格枚举只作为快照和审计解释。
3. `VEN-001` 保留一个规则 ID，但按 `expired`、`inconsistent`、`unreadable` 区分阻断或人工核验处置。
4. 一期中已过期质量证书继续由 `VEN-003` 命中并交质量经理复核，不新增第八条规则；后续企业策略可将负数改为直接阻断。

以上四项已由用户于 2026-08-27 确认。对应版本化规则配置为 `policies/rules/v1.0.0.yaml`；当前仍未实现规则引擎、数据库、接口或 Agent。
