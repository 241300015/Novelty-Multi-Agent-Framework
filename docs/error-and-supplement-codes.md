# 错误码与补检／恢复动作

当前目录包含 118 个错误码（27 个原分类和 91 个具体原因码），以及 13 个恢复动作码。错误码描述可核实的失败事实；建议动作不等于实际执行，重试上限不等于必然重试。

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

错误事件还记录对象范围、证据引用、因果事件和重试约束。原分类保留用于兼容及未知细因回退；`model.usage_unavailable` 仍只有目录定义，不能算作已覆盖的生产事件。

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

## 统一分类规则与 Harness 约束

所有错误码统一按“在哪个节点、哪一项检查失败”划分。两个引文例子不是特殊处理范围。具体码回答可核实的失败事实，`scope.node` 记录此次发现问题的位置；共用检查也可能在 Gate A 中发现 `reviewer.reference.*` 错误。

- 能从异常类型、字段检查、原始输入或读取记录确认的原因，用具体码。不能确定原因时，保留粗分类或 `execution.unknown`，不能依赖模型自述猜测根因。
- 错误码与恢复动作分开。新码通过 `failure_family()` 继承原有恢复建议和次数上限，总图按父类处理；具体码不扩大重试次数或补检权限。
- 27 个原码保留，历史 JSON 仍可读取。新增字段有默认值，未填写 `scope.node` 的历史事件 ID 保持不变。旧版本程序不保证能读取新枚举。
- HTTP 401、403、404、429、5xx 继续采用已有明确分类，只表示 HTTP 边界事实；不能进一步推断密钥过期、封禁或网站不存在等根因。
- `model.timeout`、上下文超限、计数不可用、已执行检索零候选、证据数量不足、技术特征缺口等已有明确含义的码保留。`provider.network`、`material.unavailable`、`model.output_schema`、`model.output_reference`、`review.summary_failed` 等作为未细分原因或旧数据的回退，不把它们当成完整诊断。

### 各节点具体码

下面的表由代码目录逐项列出。每个具体码均在相应生产分支或共享断言中使用；这不代表仓库每个异常点都已接入统一错误事件。

| 错误码 | 可核实的失败事实 | 兼容的原分类 |
| --- | --- | --- |
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

当前目录合计 **118 个码**（27 个原码、91 个具体码），恢复动作仍为 13 个。数量用于核对目录，不代表错误覆盖率。

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
