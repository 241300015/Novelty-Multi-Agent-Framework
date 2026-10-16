# 当前错误码与补检／恢复动作码

核查日期：2026-09-28；代码基线：`7e71dc9`。本文记录现状，不表示完整闭环已交付。

**当前有 27 个错误码、13 个恢复动作码；独立的补检原因码目录尚未建立。总图恢复策略会直接生成其中 7 种动作。** 错误事件中的建议动作不等于已执行动作，目录重试上限也不等于总图必然自动重试。

## 错误码（FailureCode）

来源：[定义与目录](../backend/src/novelty_agent_framework/schemas/failures.py)。下表是目录规定，不是全部生产路径的覆盖证明。

| 错误码 | 含义 | 层级 | 允许的恢复建议（首项为默认） | 最多追加重试 | 结论影响 |
| --- | --- | --- | --- | --- | --- |
| `provider.authentication` | 来源身份认证失败 | `provider` | `change_provider`, `manual` | 0 | `limits_coverage` |
| `provider.authorization` | 来源授权不足 | `provider` | `change_provider`, `manual` | 0 | `limits_coverage` |
| `provider.rate_limit` | 来源限流 | `provider` | `retry_request`, `change_provider`, `stop` | 1 | `limits_coverage` |
| `provider.network` | 来源网络故障 | `provider` | `retry_request`, `change_provider`, `stop` | 1 | `limits_coverage` |
| `provider.service` | 来源服务故障 | `provider` | `retry_request`, `change_provider`, `stop` | 1 | `limits_coverage` |
| `provider.protocol` | 来源协议或响应异常；分类不等于根因已定位 | `provider` | `change_provider`, `manual` | 0 | `limits_coverage` |
| `provider.resource_missing` | 来源资源不存在 | `provider` | `change_provider`, `manual` | 0 | `limits_coverage` |
| `provider.unknown` | 来源故障未分类 | `provider` | `manual`, `stop` | 0 | `limits_coverage` |
| `material.unavailable` | 材料不可用 | `reader` | `fetch_fulltext`, `manual` | 0 | `limits_coverage` |
| `material.integrity` | 材料完整性异常 | `reader` | `manual`, `stop` | 0 | `blocks_card` |
| `tool.scope` | 工具访问超出允许范围 | `harness` | `repair_arguments`, `stop` | 1，需改变输入 | `blocks_card` |
| `tool.arguments` | 工具参数不合法 | `harness` | `repair_arguments`, `stop` | 1，需改变输入 | `limits_coverage` |
| `tool.unavailable` | 工具不可用 | `harness` | `manual`, `stop` | 0 | `limits_coverage` |
| `model.timeout` | 模型调用超时 | `model` | `restore_checkpoint`, `retry_request`, `stop` | 1 | `blocks_point` |
| `model.transport` | 模型传输失败 | `model` | `restore_checkpoint`, `retry_request`, `stop` | 1 | `blocks_point` |
| `model.context_limit` | 模型上下文超限 | `model` | `restore_checkpoint`, `stop` | 0 | `blocks_point` |
| `model.context_unavailable` | 上下文计数或准入信息不可用 | `model` | `manual`, `stop` | 0 | `blocks_point` |
| `model.output_schema` | 模型输出不符合结构 | `model` | `repair_output`, `stop` | 1，需改变输入 | `blocks_point` |
| `model.output_reference` | 模型输出引用不合法 | `reviewer` | `repair_output`, `stop` | 1，需改变输入 | `blocks_point` |
| `model.usage_unavailable` | 模型用量不可用 | `runtime` | `none` | 0 | `accounting_unknown` |
| `harness.budget_exhausted` | 执行预算耗尽 | `harness` | `stop` | 0 | `blocks_point` |
| `coverage.not_executed` | 检索未执行 | `researcher` | `research_gap`, `stop` | 0 | `limits_coverage` |
| `coverage.no_match` | 已执行查询没有命中 | `provider` | `replan_query`, `stop` | 0 | `limits_coverage` |
| `evidence.missing` | 证据缺失或数量不足 | `researcher` | `research_gap`, `stop` | 0 | `limits_coverage` |
| `review.missing_features` | Reviewer 技术特征证据缺口 | `reviewer` | `read_artifact`, `fetch_fulltext`, `research_gap`, `stop` | 0 | `blocks_point` |
| `review.summary_failed` | Reviewer 汇总失败 | `reviewer` | `resume_review`, `stop` | 1 | `blocks_point` |
| `execution.unknown` | 未分类执行异常 | `runtime` | `manual`, `stop` | 0 | `unknown` |

结论影响：`limits_coverage` 限制覆盖范围；`blocks_card` 阻断单卡；`blocks_point` 阻断查新点；`accounting_unknown` 用量核算未知；`unknown` 影响未确定。协议还允许 `blocks_run`，但当前目录没有条目采用它。

错误事件还记录对象范围、证据引用、因果事件和重试约束。尚未完成全部 27 类生产发出路径及真实恢复覆盖验收；此前审计中 `model.usage_unavailable` 仅见目录定义。

## 补检／恢复动作码（RecoveryAction）

来源：[恢复策略](../backend/src/novelty_agent_framework/core/recovery_policy.py)、[工作流](../backend/src/novelty_agent_framework/workflows/novelty.py)、[工具范围约束](../backend/src/novelty_agent_framework/tools/recovery_registry.py)。

| 动作码 | 用途 | 总图直接生成 | 当前条件或边界 |
| --- | --- | --- | --- |
| `retry_request` | 重试原来源请求 | 是 | 仅网络、限流、服务故障；没有替代来源时按点和来源最多追加一次，沿用原计划 |
| `repair_arguments` | 纠正工具参数 | 否 | 目录建议；不由 plan_recovery 自动调度 |
| `repair_output` | 纠正模型输出或引用 | 否 | 目录建议；不能据此声称单卡失败已自动纠错 |
| `change_provider` | 切换检索来源 | 是 | 已尝试查询全部技术失败；只使用显式配置且未使用的替代来源 |
| `fetch_fulltext` | 获取已有记录的全文 | 是 | 语义缺口且已有记录缺全文；绑定来源及记录，每次最多 4 条 |
| `read_artifact` | 补读已有材料 | 是 | 绑定明确材料和命名空间；避免重复恢复读取 |
| `replan_query` | 重新规划查询 | 是 | 查询成功但零候选；交由模型规划新方向 |
| `research_gap` | 针对未解决缺口补检 | 是 | 证据数量或语义缺口仍未解决；补检不保证产生有效证据 |
| `resume_review` | 恢复 Reviewer 核验／汇总 | 否 | 部分独立恢复机制存在，但总图不自动生成该动作 |
| `restore_checkpoint` | 从检查点恢复 | 否 | 部分独立检查点机制存在，但总图不自动生成该动作 |
| `manual` | 人工处理 | 否 | 目录建议或终态定义；不允许追加执行次数 |
| `stop` | 停止恢复 | 是 | 轮数／预算上限，或不支持继续的技术阻塞；保留未解决状态 |
| `none` | 不采取恢复动作 | 否 | 目录终态定义；不允许追加执行次数 |

## Reviewer 补检请求现状

| 字段 | 当前用途 | 尚存缺口 |
| --- | --- | --- |
| `reason` | 自由文本补检原因 | 不是统一的补检原因码 |
| `missing_aspects` | 缺失方面，传入恢复决策及定向任务 | 仍需验证后续补检是否真正解决缺口 |
| `suggested_focus` | 模型建议的补检方向 | 当前没有独立传入 RecoveryDecision 的路径 |
| `missing_feature_ids` | 恢复策略从逐特征比较提取的缺失特征 | 不是 Reviewer 请求里的原因码；当前读取 unknown／partially_supported |

## 当前验收边界

- 总图的语义补检条件显式检查 `insufficient_evidence` 与 `semantic_evidence`；存在补查请求本身不保证触发恢复。
- 默认替代来源列表为空；`max_rounds=1` 时不会执行第二轮补检。
- 报告保存错误事件与恢复决策，但没有完整呈现每次补检的执行、结果与重新核验关联。
- 单卡运行错误、汇总恢复、研究检查点和总图之间仍有恢复边界；尚不能称端到端自动错误处理已完成。
- 历史离线测试和限定动作验证不等于真实多来源切换及语义补检效果验收。

参考：[既有覆盖审计](experiments/20260928_tool_failure_causality/code_system_status.md)、[本轮实验说明](experiments/20260928_arxiv_web_full_workflow.md)。

## 2026-10-17：Harness / Builder 实验中待归类的错误

本节仅记录已观察的问题和现有错误码的覆盖缺口，**没有新增 FailureCode 枚举、事件生产逻辑或纠错动作**。
上面的目录表仍是原核查基线；以下分类不是已经接入运行时的新错误码。按本轮要求，纠错机制暂不调整。

| 已遇到的错误类型 | 实际例子与识别依据 | 当前处理及错误码缺口 |
| --- | --- | --- |
| 选项值不属于当前菜单 | NP-1 把“执行第 1 轮检索……”的说明填入 `option_id`，而非 `v1_0`；原请求三次重放中两次复现 | Harness 记录 `unknown or stale option_id` 并拒绝执行；无法区分说明误填、旧编号和其他未知值。可归入输出契约/参数错误，尚未细分事件 |
| 引文在已读原文中不存在 | Builder 找不到 draft quote 与可信 Reader 文本的对应关系，返回 `ungrounded quote` | 已阻断产卡，但单卡拒绝主要保存在 `rejections` / warnings；现有 `model.output_reference` 不能据此视为所有 Builder 拒绝均已接入 |
| 待查新对象被误当作参考文献 | NP-1 引用输入的 GSAERU `claim_en`；修复时又引用 `technical_features_en`。此前 C 组 NP-2 也引用了 DSGNN 查新声明 | 当前仍只给出 `ungrounded quote`，未识别“查新点/参考文献来源混淆”。应与单纯抄写、改写或标点不匹配分开记录 |
| 纠正后重复同类来源错误 | 一次纠正改变了引用句子，却仍然从查新点字段选取引文，Builder 再次拒绝、恢复 0 张卡 | 只追加 `single quote correction recovered 0 card(s)` 等警告；缺少明确的修复失败分类及与首次错误的关联事件 |
| 比较对象或贡献归属偏移 | NP-2 的 AMIGA 卡引文真实，但 differences 比较 GNAN，任务目标却是 DSGNN | 引文/溯源校验可通过；属于语义错误，不能用引用合法性通过来排除。本轮未运行 Reviewer，尚无对应语义判定事件 |
| 未执行检索却声称没有命中 | NP-1 的 28 次选择均未通过，实际零检索，最终空卡理由却声称“未找到相关文献” | 应区分 `coverage.not_executed` 与 `coverage.no_match`；当前自由文本空卡理由未校对执行事实 |
| 正常收尾状态掩盖产卡修复失败 | 独立重跑 NP-1 正常返回 `completed`，但两次 Builder 尝试都失败，最后 0 张卡 | `completed` 表示调用流程结束，不能作为有效产出成功标志。暂记录为状态表达缺口，不改变任务状态契约 |
| 无效动作或重复调度消耗预算 | NP-1 反复错误编号耗尽模型轮次；旧 C NP-2 切换已有候选却重复扣检索工具次数 | 末端可表现为 `harness.budget_exhausted`，仍需保留前因。候选切换已在 `690a5b5c` 改为本地转换；编号空转尚未修复 |

当前修复入口是 [TaskResearcherWorkflow._correct_rejected_cards](../backend/src/novelty_agent_framework/workflows/research_task.py)：
把查新点、被拒草稿及错误字符串、全部 Reader 结果和输出 Schema 放进一次独立调用；使用硬编码提示、同一模型，
禁用工具，最多一次纠正，再交给 Builder 验证。它没有按上述来源混淆等类型分别组织修复输入。
JSON 格式修复与此引文修复是两个不同入口，不应混为同一次重试。

“没有读正文，但摘要引文可溯源”本身不属于引用错误：当前契约允许引用论文原始摘要，不接受模型生成摘要作为原文证据。
摘要是否足以支持比较结论属于另一层语义审查。

证据：[C 组重跑](experiments/20261017_harness_c_rerun/README.md)、
[NP-1 请求重放与独立重跑](experiments/20261017_harness_np1_reproduction/README.md)。
