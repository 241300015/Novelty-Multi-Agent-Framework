# 错误码与补检／恢复动作

当前目录包含 148 个错误码（27 个原分类和 121 个具体原因码），以及 13 个恢复动作码。错误码描述可核实的失败事实；建议动作不等于实际执行，重试上限不等于必然重试。

2026-10-17 明确维护原则：本文件是集中维护的错误码目录文档，程序定义与之同步，其他文档和 Issue 引用本目录，不另建分散码表。统一错误码用于识别问题并调用所属节点、位置和层次的本地纠错逻辑；纠错实现分别保留在 Reader、Research、Builder、Validator、Reviewer、Harness 等责任位置，不要求统一算法或集中调度。

P0-02 已将发生位置、适用上下文、拥有处理权的函数、停止边界及实现／验证状态加入程序目录；本文的逐码表与责任映射是人可读的集中目录。同一码在不同层次可有不同处理，`scope.node` 保留本次发现位置；不能仅凭建议动作宣称已自动纠错。具体分工见[后端代码审查与待办清单](后端代码审查与待办清单.md)。

## 错误码（FailureCode）

来源：[定义与目录](../backend/src/novelty_agent_framework/schemas/failures.py)。下表列出兼容历史事件的 27 个原分类，具体原因码见后文。目录不代表所有生产路径均已覆盖。

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

### 原分类码的责任与状态

下表与上方 27 行按错误码前缀联合读取。`partial` 表示分类和责任位置已定义，但各节点的完整纠错闭环仍分属 P0-03/04/05；`integration_only` 表示只有跨路径验证，不等于每个原分类都有独立发出用例。

| 错误码范围 | 触发事实 | 拥有处理权的位置 | 停止边界 | 实现 / 验证 |
| --- | --- | --- | --- | --- |
| `provider.*` | 来源适配器返回或抛出已分类事实 | `database_search.provider` / provider adapter | 仅允许已配置的有界重试或换源，否则保留局部结果并停止本操作 | `partial` / `integration_only` |
| `material.*`, `reader.*` | manifest 绑定的材料不可用或校验不一致 | `reader` / `ReferenceArtifactReaderTool.ainvoke` | 无合法替代材料或局部次数耗尽时停止，不接受未验证文本 | `partial` / `integration_only` |
| `tool.*`, `recovery.*` | 注册、参数或冻结作用域校验拒绝调用 | `ResearcherToolRegistry.execute` / `RecoveryToolRegistry.execute` | 无可修正输入或授权工具时明确停止，不执行 I/O | `partial` / `integration_only` |
| `harness.*` | Harness 决策、阶段或预算检查拒绝下一动作 | `tool_call_harness` / `ToolCallHarness._run` | 允许时只用已验证观测收尾；否则在原预算边界停止 | `partial` / `integration_only` |
| `model.*` | 模型客户端或结构化输出边界报告失败 | `ModelClient.acomplete` / 所属 Agent parser | 仅走所属 Agent 的有界修复／检查点，否则停止所影响节点 | `partial` / `integration_only`；`model.usage_unavailable` 为 `not_implemented` / `not_verified` |
| `builder.*` | Builder 溯源或引用编译拒绝草稿 | `builder.compile` / `EvidenceCardBuilder.build` | 有界补正仍无法编译时停止该 Card，保留其他有效卡 | `implemented` / `automated`（发现码） |
| `reviewer.*`, `review.*` | Reviewer 引用、比较、检查点或汇总失败 | `review_evidence` / `NoveltyEvidenceReviewer._review_point` | 保留已完成单卡；所属恢复耗尽后停止受影响 Card / point | 原码 `partial`，具体码 `implemented` / `automated` |
| `gate_a.*` | Gate A 发现 Review 缺失、重复或绑定不一致 | 发现：`NoveltyWorkflow._validate_synthesis_input`；处理权：`review_evidence` / `NoveltyEvidenceReviewer._review_point` | 不使用非法绑定汇总；回传 Reviewer 的执行闭环尚属 P0-04 | 具体码 `implemented` / `automated`（发现码） |
| `retrieval.*` | 检索预算、顺序查询或全文获取失败 | `StructuredSourceRetrievalTool.ainvoke/acquire_full_texts` | 保留元数据／摘要，该来源的局部策略耗尽后停止 | 具体码 `implemented` / `automated` |
| `coverage.*`, `evidence.*` | 执行状态证明未检索、零候选或最终有效证据不足 | `check_final_evidence_sufficiency` / `plan_recovery` | 只在局部纠错结束后进入适用的任务级补检；轮次／全局预算耗尽时停止 | `partial` / `integration_only` |
| `execution.unknown` | 已观测异常但证据不足以分类 | 发出该事件的原边界 | 停止受影响单元，人工分类前不自动恢复 | `partial` / `integration_only` |

错误事件还记录对象范围、证据引用、因果事件和重试约束。原分类保留用于兼容及未知细因回退；`model.usage_unavailable` 仍只有目录定义，不能算作已覆盖的生产事件。

## 补检／恢复动作码（RecoveryAction）

来源：[恢复策略](../backend/src/novelty_agent_framework/core/recovery_policy.py)、[工作流](../backend/src/novelty_agent_framework/workflows/novelty.py)、[工具范围约束](../backend/src/novelty_agent_framework/tools/recovery_registry.py)。

| 动作码 | 用途 | 总图直接生成 | 当前条件或边界 |
| --- | --- | --- | --- |
| `retry_request` | 重试原来源请求 | 否 | 已移出 plan_recovery；由 Research 结束处的局部纠错实现，旧代码可从 Git 历史查阅 |
| `repair_arguments` | 纠正工具参数 | 否 | 目录建议；不由 plan_recovery 自动调度 |
| `repair_output` | 纠正模型输出或引用 | 否 | 目录建议；不能据此声称单卡失败已自动纠错 |
| `change_provider` | 切换检索来源 | 否 | 已移出 plan_recovery，迁移到 Research 检索工具 |
| `fetch_fulltext` | 获取已有记录的全文 | 否 | 已移出 plan_recovery，归属 Reviewer 工具执行策略 |
| `read_artifact` | 补读已有材料 | 否 | 已移出 plan_recovery，归属 Reader 调用处现场纠错 |
| `replan_query` | 重新规划查询 | 是 | 查新点级，本轮至少一条查询成功且本轮候选总数为零；经 Coordinator LLM 规划任务，再交 Planner |
| `research_gap` | 针对未解决缺口补检 | 是 | 数量或语义缺口；经 Coordinator LLM 规划完整任务，不保证产生有效证据 |
| `resume_review` | 恢复 Reviewer 核验／汇总 | 否 | 部分独立恢复机制存在，但总图不自动生成该动作 |
| `restore_checkpoint` | 从检查点恢复 | 否 | 部分独立检查点机制存在，但总图不自动生成该动作 |
| `manual` | 人工处理 | 否 | 目录建议或终态定义；不允许追加执行次数 |
| `stop` | 停止恢复 | 是 | 轮数／预算上限，或不支持继续的技术阻塞；保留未解决状态 |
| `none` | 不采取恢复动作 | 否 | 目录终态定义；不允许追加执行次数 |

## Reviewer 补检请求现状

| 字段 | 当前用途 | 尚存缺口 |
| --- | --- | --- |
| `reason` | 作为 RecoveryDecision.review_reason 传给 Coordinator | 自由文本说明，不代替 trigger_codes |
| `missing_aspects` | 缺失方面，传入恢复决策及定向任务 | 记录追加尝试及剩余缺口，不以消除缺口作为补检成功前提 |
| `suggested_focus` | 语义缺口时传入 RecoveryDecision，供 Coordinator 参考 | 建议本身不触发补检 |
| `missing_feature_ids` | 恢复策略从逐特征比较提取的缺失特征 | 不是 Reviewer 请求里的原因码；当前读取 unknown／partially_supported |

## 当前验收边界

- 总图的语义补检条件显式检查 `insufficient_evidence` 与 `semantic_evidence`；存在补查请求本身不保证触发恢复。
- 默认替代来源列表为空；`max_rounds=1` 时不会执行第二轮补检；局部换来源不再通过补检主链调度。
- 三类补检码各自进入独立 Coordinator Harness 工作流，由程序选择对应可执行策略；同码可覆盖多个点，同点多码保留独立决策和任务，task_type 分别为 research_gap / feature_supplement / replan_query。Coordinator 采用 choice-only：LLM 选择程序发放的 option_id，程序填写 Reader 参数并执行，阶段结束后 LLM 生成决策及任务。非法选项沿用已有 Harness 错误码；数据契约与任务校验边界见[任务级补检工作规范](任务级补检数据契约与Coordinator工作规范.md)。规划失败使用既有模型／输出错误码记录，转 STOP，不能伪造规则任务兜底。
- 报告保存错误事件、任务级恢复决策和纠错索引引用；索引保留已接入本地纠错的执行、结果与重新核验。未接入节点不会被伪造成已执行。
- 单卡运行错误、汇总恢复、研究检查点和总图之间仍有未完成的业务回传边界；P0-02 的统一契约和可查询记录不等于端到端自动错误处理已完成。
- 历史离线测试和限定动作验证不等于真实多来源切换及语义补检效果验收。

## 统一分类规则与 Harness 约束

所有错误码统一按“在哪个节点、哪一项检查失败”划分。两个引文例子不是特殊处理范围。具体码回答可核实的失败事实，`scope.node` 记录此次发现问题的位置；共用检查也可能在 Gate A 中发现 `reviewer.reference.*` 错误。

- 能从异常类型、字段检查、原始输入或读取记录确认的原因，用具体码。不能确定原因时，保留粗分类或 `execution.unknown`，不能依赖模型自述猜测根因。
- 错误码与恢复动作分开。新码通过 `failure_family()` 继承原有恢复建议和次数上限，总图按父类处理；具体码不扩大重试次数或补检权限。
- 27 个原码保留，历史 JSON 仍可读取。新增字段有默认值，未填写 `scope.node` 的历史事件 ID 保持不变。旧版本程序不保证能读取新枚举。
- HTTP 401、403、404、429、5xx 继续采用已有明确分类，只表示 HTTP 边界事实；不能进一步推断密钥过期、封禁或网站不存在等根因。
- `model.timeout`、上下文超限、计数不可用、已执行检索零候选、证据数量不足、技术特征缺口等已有明确含义的码保留。`provider.network`、`material.unavailable`、`model.output_schema`、`model.output_reference`、`review.summary_failed` 等作为未细分原因或旧数据的回退，不把它们当成完整诊断。

### 各节点具体码

下面的表由代码目录逐项列出。每个具体码均在相应生产分支或共享断言中使用；这不代表仓库每个异常点都已接入统一错误事件。

Coordinator → Planner 的首轮原始任务和补检任务共用 `validate_supplement_tasks()`。
`planning_type=initial` 以全部原始查新点为目标，要求 `literature_search` 和第 1 轮，
不生成补检决策、不消耗补检轮次；补检按获准决策及触发码限定目标与任务类型。
双方共用点范围、唯一性、语言、类型、描述非空及目标覆盖检查。任务唯一键为
`(novelty_point_id, task_id)`；不同点复用 `T-1` 合法，同一规划类型内同点同语言不得重复。
初始任务 ID 保留，补检模型草稿沿用程序分配 ID／轮次，正式补检结果再次核对身份。
以下 `coordinator.task.*` 码通过 `CodedFailure` 传递，异常文本及运行错误日志保留码；
首轮失败在启动 Planner 前抛出，补检沿用对应触发码 STOP 和 FailureEvent 记录。
这些码继承 `model.output_schema` 的建议，不代表已实现自动纠错，也不做任务语义质量审查。

Planner 内部纠错由 `SearchPlannerAgent._plan()` 执行，同步及异步入口共享逻辑：

- `max_attempts` 仍是总尝试上限（默认 3），另设任务内硬上限：临时调用失败最多重试一次，草稿最多修复一次，修复越界后最多进行一次受限协议纠正；更换错误码不会重置计数，协议纠正不增加总预算。
- `model.timeout`、`model.transport`、HTTP 429／5xx 可以保持原请求重试，默认等待 0.5 秒，遵守更长的 `Retry-After`；要求等待超过 30 秒则本次停止。网络失败不进入模型修复反馈，也不覆盖之前的失败草稿。
- JSON、schema、现有 DraftIssue 校验及禁用工具调用错误交给任务内的 [PlannerRepairHarness](../backend/src/novelty_agent_framework/core/planner_repair.py)。程序按具体错误码生成修复动作、字段范围及绑定本任务/失败草稿的选项 ID；没有注册的动作或缺少必要字段位置时停止，不从报错文案猜测修改范围。
- 模型在一次响应中返回 `option_id + draft`，选择执行已授权修复或停止；不额外增加选择调用。运行时按 `prepared → scope_checked → revalidating → succeeded/failed` 推进；选择合法且改动未越界后，才执行原 schema 校验与编译器检查。仅提示词要求“只改相关字段”不算通过范围门控。
- 词项、别名、排除词错误只开放对应概念字段；策略引用只开放对应 `focus_concepts`；概念集合重建允许同步调整现有策略的引用，不允许顺带改策略层级。schema 错误开放其字段路径；JSON 无法解析或工具调用违规发生在草稿检查前，明确授权重建整份草稿。可选字段的缺省值与显式默认值等价。结构范围通过不代表贡献归属、比较对象等语义问题已经核实。
- `planner.repair.scope_violation` 表示修复越界，发现节点为 `repair_admission`。程序拒绝该草稿并保留最初失败的原稿；若总预算尚有余额，最多切换一次到 `option_id + updates` 协议。`updates` 只能提交程序发放的字段编号及新值，字段路径、替换/删除操作与类型由程序决定；重叠路径合并，额外字段用 `null` 表示获准删除。程序从原稿组装，其他字段保持不变，再运行原 schema 与编译器检查。模型不能自行指定路径或操作，也不能复用上一阶段的选项 ID。
- 受限纠正再次返回非法协议、无效草稿或编译错误时停止，不重置修复次数。`planner.repair.declined` 表示模型选择停止；非法选择使用既有 `harness.decision.*` 具体码，均直接停止。反馈包含失败原稿、全部问题、结构化位置及实际校验上限；越界纠正另含被拒响应和越界路径。默认三次调用可用于“首次草稿 → 定向修复 → 受限纠正”；若网络重试已占用预算，不额外追加调用。
- 凭据、认证／授权、其他 HTTP 拒绝、上下文、调用预算、服务协议异常、明确长度截断及程序内部错误不原样重试。未知异常保留异常链，使用 `execution.unknown`，不当成草稿问题。
- `planner.draft.*` 与现有 DraftIssue 后缀一一对应，`failure_code` 属性映射统一目录；旧后缀继续兼容。原规则的适用条件不变，空列表通常先触发 schema 校验；不新增语义质量审查。
- `SearchPlannerExhaustedError` 保留历史类名，同时表示不可重试终止或次数耗尽；携带实际尝试次数、具体 code、terminal_reason 和逐次动作历史。运行错误日志记录结构化 audit，Planner 事件记录失败输出、发现节点、授权动作与范围、实际选择与改动，以及复核 `not_run/passed/failed`。`repair_protocol_restricted` 记录协议切换，审计保留纠正次数、字段编号及被拒路径。通过确定性检查不等于保证模型生成内容的语义质量；本轮没有增加新的语义质量审查。
- 最终失败直接抛出到工作流，运行标为 FAILED，不制造 TaskResearchResult，不把规划失败转成数量补检；本批 Researcher 不启动。同批异步任务取消，已发出的底层线程请求仍可能完成。

`model.output.truncated` 虽兼容输出结构类，Planner 当前本地策略为停止；编译器内部错误
`planner.compile.invalid` 兼容未知执行类且不允许模型修复。错误码目录中的建议不是自动重试授权。

2026-10-18 首批本地真实模型验证覆盖 `model.output.schema_invalid`、`model.output.json_invalid`、
`planner.draft.exclude_conflicts_positive_concept`、`planner.draft.undefined_concept` 的受控修复，
以及当时越界即终止策略下的 `planner.repair.scope_violation` 与整批派发阻断。模型为 `qwen2.5-7b-instruct`，
5 次修复均通过复核，2 次越界注入均按预期拒绝。原始草稿与修复响应均来自真实调用，
故障由实验明确注入；这不构成自然错误率或一般修复成功率的估计。范围与记录位置见[数据链说明](data_flow.md)。

随后增加受限协议纠正，最终版本本地真实验证 23 次调用、7 个场景检查符合预期：四种字段修复成功，
非法字段编号由 `harness.decision.schema_invalid` 拒绝，成功/失败批分别派发 2/0 项。
探索时另有一次概念集合被模型清空，已由 `model.output.schema_invalid` 拦截并在第三次调用后停止；
补清集合字段说明后复测通过。探索的 15 次调用及失败样本一并保留，详见上述数据链说明。

| 错误码 | 可核实的失败事实 | 兼容的原分类 |
| --- | --- | --- |
| `planner.draft.empty_concepts` | 检索概念列表为空 | `model.output_schema` |
| `planner.draft.empty_strategies` | 检索策略列表为空 | `model.output_schema` |
| `planner.draft.blank_term` | 概念词项全部为空白 | `model.output_schema` |
| `planner.draft.too_many_concepts` | 概念数量超过配置上限 | `model.output_schema` |
| `planner.draft.too_many_terms` | 概念词项数量超过配置上限 | `model.output_schema` |
| `planner.draft.too_many_alias` | 概念别名数量超过配置上限 | `model.output_schema` |
| `planner.draft.too_many_exclude` | 排除词数量超过配置上限 | `model.output_schema` |
| `planner.draft.sentence_like_term` | 词项词数超过配置上限 | `model.output_schema` |
| `planner.draft.generic_term` | 词项全部命中当前通用词规则 | `model.output_schema` |
| `planner.draft.exclude_conflicts_positive_concept` | 排除词与正向概念词项或别名冲突 | `model.output_schema` |
| `planner.draft.missing_en_term` | 英文任务无满足当前检测规则的英文词项 | `model.output_schema` |
| `planner.draft.missing_escape` | 启用角色检查时缺少 escape | `model.output_schema` |
| `planner.draft.missing_object_method` | 启用角色检查时缺少 object 或 method | `model.output_schema` |
| `planner.draft.strategy_levels` | 普通检索策略层级未恰好覆盖 strict/medium/broad | `model.output_schema` |
| `planner.draft.duplicate_level` | 补检策略层级重复 | `model.output_schema` |
| `planner.draft.undefined_concept` | 策略引用未定义或非法编号的概念 | `model.output_schema` |
| `planner.output.tool_call_forbidden` | Planner 在草稿输出阶段返回工具调用 | `model.output_schema` |
| `model.output.truncated` | 服务明确标记输出因长度限制截断 | `model.output_schema` |
| `planner.compile.invalid` | 确定性编译阶段内部约束失败，不属于草稿规则反馈 | `execution.unknown` |
| `planner.repair.scope_violation` | 修复修改了未授权的草稿字段 | `model.output_schema` |
| `planner.repair.declined` | 模型选择停止，未提交定向修复 | `model.output_schema` |
| `coordinator.task.point_duplicate` | 查新点 ID 重复，无法唯一绑定任务 | `model.output_schema` |
| `coordinator.task.point_out_of_scope` | 任务引用未知或未获准规划的查新点 | `model.output_schema` |
| `coordinator.task.duplicate` | 同点任务 ID 重复，或同一规划类型内同点同语言任务重复 | `model.output_schema` |
| `coordinator.task.language_invalid` | 任务语言未启用 | `model.output_schema` |
| `coordinator.task.type_invalid` | 任务类型不符合当前规划类型 | `model.output_schema` |
| `coordinator.task.description_missing` | 任务描述为空 | `model.output_schema` |
| `coordinator.task.coverage_missing` | 未为全部获准目标点生成任务 | `model.output_schema` |
| `coordinator.task.binding_invalid` | 任务、查新点或规划决策绑定不一致 | `model.output_schema` |
| `coordinator.task.identity_invalid` | 任务编号或轮次不符合规划约定 | `model.output_schema` |
| `builder.artifact.missing` | 索引缺少 Artifact | `model.output_reference` |
| `builder.artifact.work_mismatch` | Artifact 与读取结果的 Work 不一致 | `model.output_reference` |
| `builder.card.duplicate_work` | 同一文献重复产卡 | `model.output_reference` |
| `builder.correction.native_call_forbidden` | 纠错返回了禁用的工具调用 | `model.output_schema` |
| `builder.correction.too_many_cards` | 纠错输出卡数多于已读文献数 | `model.output_schema` |
| `builder.quote.from_target_input` | 无法匹配参考文献，但匹配了输入查新点内容 | `model.output_reference` |
| `builder.quote.generated_summary` | 引文仅命中了模型生成的摘要 | `model.output_reference` |
| `builder.quote.near_match` | 引文仅与已读材料近似匹配，字符存在差异；不推断抄写动机 | `model.output_reference` |
| `builder.quote.unmatched` | 引文不在已读的合法参考材料中，具体来源未知 | `model.output_reference` |
| `builder.source.ambiguous` | 引文可同时绑定多个文献，无法唯一溯源 | `model.output_reference` |
| `builder.source.cross_work` | 引文不能共同绑定到一篇文献，且无法安全拆卡 | `model.output_reference` |
| `builder.source.web_supplement` | 补充网页不能用作论文证据 | `model.output_reference` |
| `builder.source_record.missing` | Artifact 引用了不存在的 SourceRecord | `model.output_reference` |
| `builder.work.missing` | 索引缺少 Work | `model.output_reference` |
| `gate_a.read.evidence_unregistered` | 阅读引用缺少已登记的增量证据 | `model.output_reference` |
| `gate_a.read.unverified` | 阅读引用缺少核验通过的 Reader 记录 | `model.output_reference` |
| `gate_a.review.accepted_evidence_missing` | 已完成的审阅没有合法 Card 或 Evidence | `model.output_reference` |
| `gate_a.review.duplicate` | 查新点有重复 Review | `model.output_reference` |
| `gate_a.review.missing` | 查新点没有 Review | `model.output_reference` |
| `gate_a.review.status_conflict` | 已完成状态仍带未完成原因 | `model.output_reference` |
| `gate_a.review.unresolved_failure` | 已完成状态仍带阻断执行错误 | `model.output_reference` |
| `gate_a.verdict.coverage_incomplete` | 不新颖结论缺少单篇文献的完整特征支持 | `model.output_reference` |
| `harness.budget.per_tool_exhausted` | 单工具调用次数耗尽 | `harness.budget_exhausted` |
| `harness.budget.read_chars_exhausted` | 读取字符预算不足或耗尽 | `harness.budget_exhausted` |
| `harness.budget.tool_calls_exhausted` | 总工具调用次数耗尽 | `harness.budget_exhausted` |
| `harness.budget.turns_exhausted` | 模型探索轮次耗尽 | `harness.budget_exhausted` |
| `harness.decision.label_instead_of_id` | 返回了当前菜单的标签而非编号 | `tool.arguments` |
| `harness.decision.multiple_tool_calls` | 串行模式返回多个工具调用，只保留首个 | `tool.arguments` |
| `harness.decision.native_call_forbidden` | 选项模式下模型仍返回原生工具调用 | `tool.arguments` |
| `harness.decision.schema_invalid` | 决策 JSON 或字段结构不合法 | `tool.arguments` |
| `harness.decision.stale_option` | 返回了曾经发放但当前已经失效的编号 | `tool.arguments` |
| `harness.decision.unknown_option` | 返回了未发放的编号 | `tool.arguments` |
| `harness.stage.premature_finish` | 仍有必需阶段时提前提交草稿 | `tool.scope` |
| `harness.stage.read_required` | 检索后未读取必需材料 | `tool.scope` |
| `model.budget.calls_exhausted` | 模型调用预算耗尽 | `harness.budget_exhausted` |
| `model.config.alias_unknown` | 模型别名未注册 | `model.transport` |
| `model.config.credential_missing` | 模型客户端缺少配置凭据 | `model.transport` |
| `model.config.provider_unsupported` | 客户端不支持配置的供应商 | `model.transport` |
| `model.output.json_invalid` | 模型输出不是合法 JSON | `model.output_schema` |
| `model.output.schema_invalid` | JSON 已解析，但字段或类型违反数据契约 | `model.output_schema` |
| `model.request.authentication_failed` | 模型接口返回 HTTP 401 | `model.transport` |
| `model.request.authorization_failed` | 模型接口返回 HTTP 403 | `model.transport` |
| `model.request.http_rejected` | 模型接口返回其他非成功 HTTP 状态 | `model.transport` |
| `model.request.rate_limited` | 模型接口返回 HTTP 429 | `model.transport` |
| `model.request.server_error` | 模型接口返回 HTTP 5xx | `model.transport` |
| `model.response.arguments_json_invalid` | function.arguments 字符串不是合法 JSON | `model.transport` |
| `model.response.arguments_not_object` | function.arguments 解码后不是对象 | `model.transport` |
| `model.response.arguments_not_string` | function.arguments 不是字符串 | `model.transport` |
| `model.response.content_type_invalid` | 响应 content 不是字符串或 null | `model.transport` |
| `model.response.envelope_invalid` | 响应不符合 chat completions 的 choices/message 契约 | `model.transport` |
| `model.response.function_missing` | tool_call 缺少 function 对象 | `model.transport` |
| `model.response.function_name_missing` | function 缺少有效 name | `model.transport` |
| `model.response.tool_call_id_missing` | tool_call 缺少有效 id | `model.transport` |
| `model.response.tool_call_not_object` | 单项 tool_call 不是对象 | `model.transport` |
| `model.response.tool_calls_not_array` | 响应 tool_calls 不是数组 | `model.transport` |
| `provider.credentials.missing` | 配置中缺少凭据 | `provider.authentication` |
| `provider.request.connection_failed` | 请求明确抛出连接异常 | `provider.network` |
| `provider.request.timeout` | 请求明确抛出超时异常 | `provider.network` |
| `provider.response.contract_invalid` | 返回数据不能满足解析契约 | `provider.protocol` |
| `reader.artifact.hash_mismatch` | 落盘文件的 SHA-256 与索引不一致 | `material.integrity` |
| `reader.artifact.path_escape` | 材料路径越过允许的目录 | `material.integrity` |
| `reader.artifact.unregistered` | 绑定的 artifact 不在索引中 | `material.unavailable` |
| `reader.artifact.verification_failed` | 文件校验失败，尚未细分校验器原因 | `material.integrity` |
| `reader.content.decode_failed` | 内容无法按 UTF-8 解码 | `material.integrity` |
| `reader.file.access_denied` | 文件访问被拒绝 | `material.unavailable` |
| `reader.file.missing` | 文件不存在 | `material.unavailable` |
| `reader.manifest.invalid` | 索引文件无法解析或不符合契约 | `material.integrity` |
| `recovery.scope.changed` | 恢复调用改变了冻结的原始范围 | `tool.scope` |
| `retrieval.budget.requests_exhausted` | 检索请求预算耗尽 | `harness.budget_exhausted` |
| `retrieval.fulltext.acquisition_failed` | 全文获取抛出异常，具体原因见关联事件 | `material.unavailable` |
| `retrieval.fulltext.empty` | 全文调用成功返回但没有可读内容 | `material.unavailable` |
| `retrieval.fulltext.unsupported` | 来源没有全文获取能力 | `material.unavailable` |
| `retrieval.query.skipped_after_failure` | 前置检索失败后本查询未执行 | `coverage.not_executed` |
| `reviewer.checkpoint.invalid` | 汇总检查点输入无法核验 | `review.summary_failed` |
| `reviewer.checkpoint.write_failed` | 保存汇总检查点失败 | `review.summary_failed` |
| `reviewer.comparison.evidence_binding_invalid` | 比较证据缺失或属于其他 Work | `model.output_reference` |
| `reviewer.comparison.evidence_missing` | 明确比较结论没有证据 | `model.output_reference` |
| `reviewer.comparison.unknown_feature` | 比较了未定义的特征 | `model.output_reference` |
| `reviewer.comparison.work_out_of_scope` | 比较了范围外的 Work | `model.output_reference` |
| `reviewer.reference.card_evidence_unlinked` | Card 与引用证据没有关联 | `model.output_reference` |
| `reviewer.reference.duplicate_evidence` | 增量证据编号重复 | `model.output_reference` |
| `reviewer.reference.evidence_id_collision` | 增量证据编号与原始证据冲突 | `model.output_reference` |
| `reviewer.reference.evidence_out_of_scope` | 增量证据越过 Card 或 Work 范围 | `model.output_reference` |
| `reviewer.reference.point_out_of_scope` | Review 指向其他查新点 | `model.output_reference` |
| `reviewer.reference.unknown_card` | 引用未知 Card | `model.output_reference` |
| `reviewer.reference.unknown_evidence` | 引用未知 Evidence | `model.output_reference` |
| `reviewer.reference.work_mismatch` | Work 与引用证据不一致 | `model.output_reference` |
| `reviewer.summary.result_write_failed` | 保存汇总尝试结果失败 | `review.summary_failed` |
| `tool.arguments.schema_invalid` | 调用参数未通过数据契约 | `tool.arguments` |
| `tool.dispatch.not_registered` | 工具未注册 | `tool.unavailable` |
| `tool.scope.denied` | 工具拒绝当前任务范围 | `tool.scope` |

当前目录合计 **148 个码**（27 个原码、121 个具体码），恢复动作仍为 13 个。数量用于核对目录，不代表错误覆盖率。

#### 具体码的责任联表

上方 121 行“具体码”与下表按最长前缀联合后，即得到每个码的发生节点、处理权函数和停止边界。程序中 `FAILURE_CATALOG[code]` 已把联表结果展开为每码元数据，自动测试要求 148 个定义全部非空。表中 `implemented / automated` 只说明该具体失败事实有程序发出／断言和自动用例，**不表示对应业务纠错已全部自动闭环**。

| 最长前缀 | 发现节点 | 拥有处理权的函数 | 具体码实现 / 验证 | 停止边界 |
| --- | --- | --- | --- | --- |
| `builder.` | `builder.compile` | `EvidenceCardBuilder.build` | `implemented / automated` | 有界补正后仍无法编译则停止该卡 |
| `reviewer.` | `review_evidence` | `NoveltyEvidenceReviewer._review_point` | `implemented / automated` | 保留已完成单卡，有界恢复耗尽后停止受影响结果 |
| `gate_a.` | `validate_synthesis_input` | `NoveltyEvidenceReviewer._review_point`（处理权） | `implemented / automated` | 不让非法 Review 绑定进入汇总；回传执行属 P0-04 |
| `reader.` | `reader` | `ReferenceArtifactReaderTool.ainvoke` | `implemented / automated` | 无合法替代或局部预算耗尽，不接受未核验文本 |
| `retrieval.fulltext.` | `database_search.acquire_full_texts` | `StructuredSourceRetrievalTool.acquire_full_texts` | `implemented / automated` | 保留已验证摘要，停止该来源记录的全文获取 |
| `retrieval.` | `database_search` | `StructuredSourceRetrievalTool.ainvoke` | `implemented / automated` | 请求预算耗尽或前置失败时停止，不写成零候选 |
| `provider.` | `database_search.provider` | provider search / metadata / full-text adapter | `implemented / automated` | 无已配置重试／换源时保留局部结果并停止 |
| `harness.` | `tool_call_harness` | `ToolCallHarness._run` | `implemented / automated` | 预算不增补；只用已验证观测收尾或停止 |
| `tool.` | `researcher_tool_registry` | `ResearcherToolRegistry.execute` | `implemented / automated` | 返回结构化拒绝；无修正输入／授权工具时停止 |
| `recovery.` | `recovery_registry` | `RecoveryToolRegistry.execute` | `implemented / automated` | 拒绝改变冻结范围的调用，不执行 I/O |
| `model.` | `model_call` | `ModelClient.acomplete` / owning Agent parser | `implemented / automated` | 所属 Agent 有界修复／检查点不可用时停止 |

没有落入上表的 27 个原分类码按前节原码责任表处理；详细触发文字由同一 `FAILURE_CATALOG` 提供。

### 错误记录的数据通路

| 位置 | 结构化记录 | 如何向下传递 |
| --- | --- | --- |
| Harness 决策与预算 | `ToolCallHarnessEvent.failure_code` | 随 trace 留存并进入任务的 `harness_diagnostics`；预算终止另进入任务执行错误 |
| Provider / Reader / 工具边界 | `FailureEvent.code`、`scope.node`、对象范围及已有因果引用 | 工具 observation → TaskResearchResult / Review → 恢复策略与报告 |
| Builder 首次编译 | `EvidenceCardBuildRejection.code`、`node`、`card_index`、`reason` | `TaskResearchResult.card_build_rejections`，随任务结果落盘 |
| Builder 纠错后编译 | 同上，`node=builder.correction` | `card_correction_rejections`；索引属于纠错输出，不是原始草稿索引 |
| 纠错调用 / 格式失败 | `FailureEvent` | `card_correction_failure`；保留首次拒卡记录 |
| Reviewer 引用检查 / Gate A | 带代码的断言 → `FailureEvent` | `Review.execution_issues`；发现位置在 `scope.node` 中 |

Builder 的首次拒卡记录是尝试历史，不等于恢复后仍未解决的错误；不会把已经修好的卡因旧拒卡记录再次阻断。没有新增卡也不自动解释成同一种错误：看纠错拒卡记录、纠错异常和最终卡数量。当前 completed 状态仍仅表示调用流程结束，可能没有有效卡。

### 纠错生命周期与查询索引（Issue #21）

`FailureEvent` 仅记录失败事实和默认建议；`RecoveryDecision` 仅记录任务级下一动作。实际纠错由独立的 `CorrectionRecord` 记录，不用前两者的存在推断“已执行”。

```text
detected
  ├─ advised → executing → verification_pending → verification_passed
  │                         └→ attempt_failed → advised / executing / stopped
  ├─ executing → ...
  └─ stopped
```

`verification_passed` 和 `stopped` 为终态，不允许回退或改写。`verification_passed` 必须有复核器、时间和通过结果，且不得保留未解决项；`attempt_failed` 必须保留复核失败和剩余问题；`stopped` 必须有明确停止原因。

每个快照包含：

- `run_id / paper_id / point_id / task_id`、发生节点和处理函数；
- 来源、SourceRecord、材料、Card、Evidence 句柄；
- FailureCode、RecoveryAction、真实 action input、局部／全局预算和尝试次数；
- 纠错前后产物命名空间、ID、版本和 SHA-256；
- 执行结果、重新验证、剩余问题及停止原因。

本地纠错的 `supplement_round_before` 和 `supplement_round_after` 必须相同；记录器不会重置或增加全局预算。不对所有码规定同一重试次数，具体上限仍由所属节点和 `FailureDefinition.retry_limit` 限制。

查询路径：

| 查询面 | 内容 |
| --- | --- |
| `runtime/<run_id>/corrections/events/*.json` | 不可覆盖的每次状态转移 |
| `runtime/<run_id>/corrections/index.json` | 每条 correction 最新快照、全部事件路径、状态计数和未解决 ID |
| `runtime/<run_id>/manifest.json` | `correction_index.path` 的稳定入口 |
| `runtime/<run_id>/summary.json` | correction 状态计数、未解决 ID 和事件数 |
| Web `RunSnapshot.progress.corrections` | 运行中的计数、未解决 ID 和事件数 |
| Web `RunReport.corrections_url` / `GET /runs/{task_id}/corrections` | 报告就绪后查询同一任务的完整纠错索引；不将审计路径混入业务 `NoveltyReport` |

可显式运行 Issue #21 live 验收：

```bash
pytest -m live tests/test_live_correction_trace.py -q
pytest -m live tests/test_live_arxiv_tools.py::test_live_arxiv_search_fetch_resolve -q -s
```

第一条使用真实 Runtime 文件、FastAPI middleware 和路由，不需外部凭据；第二条访问真实 arXiv 网络，不需 API Key。两者都不调用付费模型。

当前已在 `ResearcherToolRegistry` 接入“参数／作用域错误 → 反馈修正 → 真实执行 → 复核”，并在 `ToolCallHarness` 接入预算耗尽终态。Reader、Builder、Validator、Reviewer、Gate A 其余跨节点回传仍由 P0-03/04 在原责任位置接入；本索引不代替它们的纠错实现。

### 引文分类边界

先走原来的严格引文匹配；真正匹配到参考材料时，即使输入查新点也有相同语句，仍按合法参考来源处理。只有匹配失败，才分类为：

1. 引文出现在输入的 claim / technical_features（含英文对应字段）中：`builder.quote.from_target_input`。
2. 引文长度至少 40 字符、已读文本存在局部片段，按字符差异计算仅有最多 3 个字符变化：`builder.quote.near_match`。这是诊断近似关系，不证明只是抄写错误。
3. 无法确定来源：`builder.quote.unmatched`。

近似匹配绝不用于接受证据、自动改引文或填来源。把待查新对象的贡献归给参考文献、把比较对象写错等语义错误，尚未进入可靠的自动识别和纠错流程；错误码目录本身不能证明已经解决。

### Harness 正式请求契约

`text_choices` 模式每轮由 Harness 直接构建 `response_format=json_schema`：仅允许当前菜单编号，要求 `option_id`，禁止额外字段，同时关闭原生 tools。编号与标签、旧编号与未发放编号分开记录。

菜单为空和预算收尾时恢复调用方的草稿输出契约；纠错调用继续使用自己的原始契约。服务端若不支持该约束会显式失败，未设置静默降级为自由生成。客户端仍校验选项，服务端忽略约束也不能执行越权工具。


### 尚未接入的边界

统一错误码当前覆盖既有 FailureCode 体系及其已连接的 Researcher、工具、Reviewer、Gate A 路径。Extractor、Planner、Validator 的部分警告／拒绝原因、Renderer 等仍有独立异常或字符串记录，不能据此声称全仓库已完成错误事件覆盖。它们以后接入同样必须使用“节点＋明确失败事实”的规则，而不能合并成一个笼统的 agent_failed。

语义归属核验、自动恢复、补检算法和空卡状态判定仍各有独立边界。`model.usage_unavailable` 等只在目录中存在的历史条目仍不能计入生产发出覆盖。

### PointExtractor 持久化阅读接入（Issue #20）

`extract_points.read` 复用 `reader.file.missing`、`reader.file.access_denied`、
`reader.artifact.hash_mismatch`、`reader.artifact.path_escape`、`reader.manifest.invalid`、
`reader.content.decode_failed` 等现有码；索引无法验证时停止本节点，禁止回退内存正文。
选择参数错误按 `tool.arguments.schema_invalid` 留痕并允许一次反馈修正；预算终止记录
`harness.budget.turns_exhausted`。摘要与持久化身份不符记录 `recovery.scope.changed`。
生成和补充的来源必须来自实际读取记录，否则使用 `model.output_reference`。
处理函数、trace 位置与尚未实现的语义检查见 [全文阅读契约](point-extractor-paper-reading.md)。
该接入不新增错误码；trace 中的结构化记录不等同于全仓统一 FailureEvent 迁移完成。
