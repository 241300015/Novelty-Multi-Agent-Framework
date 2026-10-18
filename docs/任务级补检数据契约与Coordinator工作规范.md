# 任务级补检数据契约与 Coordinator 工作规范

本规范定义以 LLM 决策为核心的任务级补检工作流、数据契约及运行约束。

补检的目的是在已有查新点上增加一次完整查新机会，降低单次检索的偶然性。它不保证补齐证据数量、消除语义缺口或产生新增有效卡。验收关注触发是否正确、追加任务是否实际完成、成果与剩余不确定性是否保留，以及是否按预算和轮次停止；有效卡增量只作为观察指标。各节点的现场纠错由各自机制承担。

Coordinator 采用“程序提供合法选项 → LLM 选择 → 程序执行并更新状态 → 指定阶段生成业务结果”的工作范式。补检触发、材料索引、动作和任务类型由 Coordinator 专属可执行策略约束。

## 1. 执行流程与职责

```text
Validator → Reviewer → Gate A
  → 数量统计 + plan_recovery（程序提取触发事实、检查停止约束）
  → recovery_decisions（coordinator_status=not_requested）
  → 路由：有任务级动作且轮次允许？
      否 → 汇总报告
      是 → Workflow._plan_supplement
             → CoordinatorRecoveryHarness 按 trigger_code 拆成独立工作流
             → 每个触发码选择一份可执行策略，包含该码涉及的全部查新点
             → Harness 生成当前 option_id 菜单，LLM 只能选择选项
             → 程序填写 Reader 参数并执行，依据结果更新菜单（可选、多轮、受限）
             → 选择结束材料核对，或预算触发程序强制收尾
             → LLM 输出 CoordinatorRecoveryDraft（决策动作、理由、读取引用、完整任务）
             → Harness 校验并绑定事实，生成最终 RecoveryDecision + ResearchTask
             → Workflow 再次校验，派发新任务
             → Planner → Researcher → Validator → Reviewer → Gate A → 再次判断
```

三类补检都经过 Coordinator 的 LLM 规划；**以触发码为独立执行单元**：同一码的全部目标查新点进入一个 Agent 工作流，可有多轮模型/Reader 交互；同一点命中多个码时，分别保留决策和任务，独立派发，不在派发前合并。原 `_targeted_supplement()` 直接按规则生成任务的路径已移除。Coordinator 的首轮 `plan()` 仍是确定性分工。`DemoCoordinator` 是离线演示替身，不调用真实模型；生产 `NoveltyCoordinatorAgent` 调用统一模型客户端。

`plan_recovery()` 自身保持同步规则函数，负责提取触发事实、检查停止约束并给出建议动作。为保持状态兼容，门控仍用 coordinator_status=not_requested 的 RecoveryDecision 承载这些事实；这不是最终规划。最终 action、reason 和任务内容由 LLM 生成，Harness 将其与只读触发事实绑定成正式 RecoveryDecision。Coordinator 不能解除 STOP 或改变预算。

## 2. 三类触发事实

| trigger_codes | 事实 | action |
| --- | --- | --- |
| `evidence.missing` | Validator 放行的最终有效卡数量低于门槛 | `research_gap` |
| `review.missing_features` | Gate A 后 Review 的 status 为 insufficient_evidence，且 incomplete_reason 为 semantic_evidence | `research_gap` |
| `coverage.no_match` | **查新点级、本轮至少一条查询 succeeded，且该点本轮所有查询的候选总数为零；不要求全部查询成功** | `replan_query` |

第三类按当前 research_tasks 的 `(point_id, task_id)` 选取搜索记录，并排除显式属于其他 run_id 的记录；重复审计记录按任务、execution_id、started_at 去重。没有 run_id 的旧记录依赖当前任务绑定。partial 不算成功；但任何查询（包括 partial）产生候选都会使总数大于零。不读取当前轮之外的空结果来重复触发第三类。直接调用 plan_recovery 而未提供 current_task_keys 时，调用方必须自行限定传入结果范围。

三个条件独立检测、独立规划。可执行决策以 `(trigger_code, point_id)` 唯一绑定，每条 trigger_codes 仅含一个码；同一点可同时有数量、语义、零候选三条决策。各条保留本码触发事件与相关技术错误，不混入其他补检码的触发事件。STOP 可汇总多个码表示程序整体禁止继续；STOP 不进入 Agent。action 不能代替具体触发码，也不能用同点合并覆盖另一类决策。

现有轮次、预算错误、Reviewer / Researcher 技术阻塞和全失败搜索停止保护继续生效。触发事实存在不等于获准执行。错误码继续只在 [统一错误码目录](error-and-supplement-codes.md) 维护。

## 3. RecoveryDecision 数据契约

程序定义：[schemas/failures.py](../backend/src/novelty_agent_framework/schemas/failures.py)。同轮每个 `(trigger_code, point_id)` 最多一条可执行决策，查新点是目标绑定而非 Agent 工作流划分单位；run_id / paper_id 记录在运行状态和审计外层。

| 字段 | 生产方 | 含义 |
| --- | --- | --- |
| point_id | 程序 | 已有查新点标识，不能由模型新增或改写 |
| round_number | 程序 | 作出判断的轮次；新增任务 attempt 为此轮次加一 |
| trigger_codes | 程序 | 可执行决策恰好一个码；历史记录默认空列表，程序 STOP 可保留多个码 |
| action | 门控给建议；LLM 生成最终补检动作 | 仅能选择本点触发码授权的 research_gap / replan_query；STOP 由程序控制 |
| reason | 门控阶段为程序；规划成功后为 LLM | 最终说明补检方向、缺口与历史尝试的区别；门控原始原因保留在 gate 审计 |
| cause_event_ids | 程序 | 关联的缺口和执行失败事件 ID；对应事件保留在 failure_events |
| missing_feature_ids | 程序从 Review 提取 | relation 为 unknown / partially_supported 的特征 ID |
| missing_aspects | Reviewer → 程序 | Reviewer 指出的缺失方面 |
| review_reason / suggested_focus | Reviewer → 程序 | 语义缺口的具体说明与建议方向；本身不构成独立触发条件 |
| max_additional_attempts | 程序 | 当前决策允许一次追加轮；STOP 为 0，不代表可重置全局预算 |
| coordinator_status | 程序在 LLM 调用后回填 | not_requested / planned / failed |
| planned_task_ids | 程序在校验后回填 | Coordinator 生成、程序编号的任务 ID，结合 point_id 唯一定位任务 |
| planning_origin | 程序 | runtime 为门控/停止；coordinator_llm 为真实模型规划；demo 为离线替身 |
| planning_basis_read_ids | LLM 提交，程序校验 | 本次 Reader 已返回、本点或论文全局、非空内容的 read_id；无读取时为空 |
| coordinator_guidance_ids | Harness | 本点实际使用的可执行策略名称、版本与定义哈希，不能由模型伪造；保留原字段名兼容已有数据 |

旧的 source_id、artifacts、source_record_ids 保留供历史记录与局部纠错工具兼容；新补检策略不使用它们来安排局部操作。

状态规则：

- 门控生成 not_requested；STOP 不进入模型规划。
- 模型输出通过校验后为 planned，并必须有非空、无重复的 planned_task_ids。
- 调用或输出校验失败时转为 STOP + failed，任务 ID 清空，追加结构化失败事件。
- 不向 LLM开放对 coordinator_status、预算或 task ID 的决定权。LLM 输出 CoordinatorRecoveryDraft，包含决策及任务；Harness 校验后绑定成可执行 RecoveryDecision。不存在只输出任务列表再由规则代写决策理由的回退。

## 4. Coordinator 输入与输出

入口：[agents/coordinator.py](../backend/src/novelty_agent_framework/agents/coordinator.py) 的 plan_supplement。

### 4.1 结构化输入：SupplementPlanningInput v2

可校验定义：[schemas/supplement_planning.py](../backend/src/novelty_agent_framework/schemas/supplement_planning.py)。组装与材料核对：[core/supplement_input.py](../backend/src/novelty_agent_framework/core/supplement_input.py)。可通过 `SupplementPlanningInput.model_json_schema()` 导出 JSON Schema；未声明字段会被拒绝。

Workflow 在调用前构建并校验契约，Coordinator 再核对契约与入口参数一致。默认提示词接收按触发码过滤后的 `planning_input_json`；不会重复发送整篇论文正文或完整证据引用文本。分项输入变量仍提供，但自定义提示词必须升级为 CoordinatorRecoveryDraft 输出，并配合 Harness 按码选择的可执行策略。v2 为材料索引增加必填 material_id，旧 v1 输入只保留作历史审计，不直接重放为带 Reader 的执行请求。

| 字段 | 类型 / 约束 | 内容与来源 |
| --- | --- | --- |
| schema_version | 固定整数 2 | 输入契约版本，与 RecoveryDecision 的版本独立 |
| run_id | string / null | Workflow 填运行 ID；脱离工作流的直接调用可以为空 |
| paper | PlanningPaper | 仅 paper_id、title、abstract，不含 full_text |
| brief | PlanningBrief | paper_summary、research_problem、keywords_zh、keywords_en，以及获准的 novelty_points；不重复携带任务列表 |
| brief.novelty_points[] | PlanningPoint | 已批准 NoveltyPoint 的字段及 feature_catalog；恰好覆盖本输入涉及的获准点；同一点可绑定不同触发码的决策 |
| recovery_decisions[] | RecoveryDecision，非空 | 仅 research_gap / replan_query、not_requested、允许追加一次的决策；STOP 不进入输入 |
| existing_evidence[] | PlanningEvidence | 仅获准点的有效卡摘要：card_id、novelty_point_id、document_title、main_contribution、overlaps、differences；不含 sources 中的完整引用 |
| insufficient_final_evidence_points[] | PlanningCountGap | novelty_point_id、valid_card_count、required_card_count、reason，以及 additional_cards_needed；只包含数量不足的点，其他触发可为空 |
| research_languages[] | zh / en，非空且无重复 | 系统启用语言，模型不可扩展 |
| attempt | integer ≥ 2 | 即将执行的补检轮次；决策 round_number 非空时必须等于 attempt − 1 |
| research_history[] | RecoveryAttempt | 获准点的历史任务及实际查询摘要，历史 attempt 必须小于本次 attempt |
| material_index[] | PersistedPlanningMaterial | 当前输入所关联、经文件内容核对的完整材料索引；无已验证索引时为空数组 |

特征目录的元素固定为 `{feature_id, text, sha256}`。F1 / F2 按该查新点 technical_features 的原始顺序编号，text 与原特征逐字一致，sha256 为该文本 UTF-8 字节的 SHA-256；F1 需要结合 point_id 使用，不能跨点混用。程序校验整个目录与获准特征一致。

数量差额满足 `additional_cards_needed = required_card_count - valid_card_count > 0`；这是有效卡缺口，不是要生成的任务数，也不是候选文献数。同一点的数量、语义、零候选原因分别保留为独立决策和工作流。

### 4.2 已持久化完整材料的索引

索引由程序生成，模型不能补写路径或哈希。每项格式如下（表中路径相对于本次配置的 output_root，不是仓库目录）：

| 字段 | 类型 / 约束 | 含义 |
| --- | --- | --- |
| material_id | M1 / M2 / …，本次输入内唯一 | Reader 只接受此句柄；不同调用不可混用 |
| kind | paper / novelty_point / evidence_card / task_result / review / recovery_decision | 被定位的完整记录类型 |
| point_id | string / null | 除 paper 外必填，必须属于获准查新点 |
| task_id、attempt | string、integer / null | 仅 task_result 必填，绑定历史任务和实际执行轮次 |
| card_id | string / null | 仅 evidence_card 必填，绑定输入中的有效卡 |
| trigger_code | 三种补检码之一 / null | 仅 recovery_decision 材料必填，防止一个工作流读取其他码的门控决策 |
| path | 相对路径 | 必须位于 output_root 内，不能使用绝对路径或 .. 逃逸；采用 / 分隔 |
| json_pointer | string | JSON Pointer；空字符串表示整个 JSON 文档；如 /accepted_evidence_cards/0 |
| sha256 | 64 位小写十六进制 | **整个文件字节**的 SHA-256，不是摘要文本或单个 JSON 对象的哈希 |

当前完整材料与索引对应关系：

| kind | 文件路径（相对 output_root） | JSON Pointer / 完整内容 |
| --- | --- | --- |
| paper | `<paper_id>/paper-input/others/paper.json` | 空字符串；完整 PaperInput，包含 full_text |
| novelty_point | `<paper_id>/novelty-points.json` | `/novelty_points/<实际下标>`；完整获准查新点 |
| evidence_card | `<paper_id>/evidence-cards.json` | `/accepted_evidence_cards/<实际下标>`；完整卡、sources 引用和 evidence_ids |
| task_result | `<paper_id>/research-runs/<point_id>/<task_id>/attempt-<历史轮次>.json` | 空字符串；完整任务结果、查询执行、候选审计及研究材料引用 |
| review | `<paper_id>/novelty-reviews.json` | `/reviews/<实际下标>`；完整点级 Review |
| recovery_decision | `<paper_id>/recovery/<run_id>/round-<判断轮次>-gate.json` | `/decisions/<实际下标>`；本轮门控的完整待规划决策 |

上述是路径规则，不是声称某个运行已经存在这些文件。实际索引只在文件可读、JSON 可解析、目标完整记录与当前状态一致时加入；不能仅凭相同 paper_id / card_id 或文件存在就认定可用。门控审计同时核对 run_id；任务结果有显式其他 run_id 的查询时不建立索引。旧的论文级文件不带 run_id，采用完整记录内容比对，不声称它们天然属于本次运行。

文件缺失、内容过期或无法验证时省略对应索引，不伪造路径、不为了补齐索引重写材料；空索引不等于材料不存在或证据不足。读取者需要重新校验文件哈希再解析 json_pointer；若文件后来被覆盖，旧索引应视为失效，不能静默读新内容。索引在调用时有效，不保证共享的论文级文件永久不可变。

这里的“完整材料”指被索引的完整业务记录：论文记录包含全文，证据卡包含完整引用，任务结果包含已有 Artifact 等材料引用。**证据卡索引不意味着来源论文的全文已经下载**；后者仍需沿已有材料引用解析并核对 Artifact。仅有 DOI / URL 不能标记成本地全文索引。Coordinator Reader 只读已索引的完整业务记录；不会自动沿引用遍历 Artifact，也不提供下载全文或检索工具。

每次调用的完整契约另存为 `<output_root>/<paper_id>/recovery/<run_id>/attempt-<本次轮次>-input.json`，供复核实际发送的摘要及索引。直接调用 Coordinator 而没有传入 Workflow 构建的 planning_input 时，使用同样的契约但 material_index 为空，不自行猜测存储路径。

### 4.3 历史摘要与输出验收

历史摘要使用 RecoveryAttempt / RecoverySearchFact：

- 任务：point_id、task_id、attempt、language、task_type、description、status、produced_card_count、failure_codes。
- 查询：execution_id、source_id、query、status、candidate_count。
- produced_card_count 是 Researcher 产出量，不能替代 Validator 后的有效卡数量；有效数量由已有卡和数量差额输入提供。
- result_missing 表示没有任务结果，不能解释成成功零候选。历史记录不等于所有现场纠错已完成。

模型输出使用 `CoordinatorRecoveryDraft`，顶层包含当前独立 `trigger_code` 及非空 decisions 数组，每个本码目标点一项：

| 字段 | 归属与约束 |
| --- | --- |
| point_id | 必须覆盖全部获准点且不能重复、越界 |
| action | LLM 在该点触发码允许的 research_gap / replan_query 中选择 |
| reason | LLM 生成具体规划理由，不能只是复制触发码 |
| basis_read_ids | LLM 引用本次实际返回的 Reader 记录；拒绝伪造、空读取和其他点引用 |
| research_tasks | 本点完整 ResearchTask 列表，至少一项 |

数量/语义工作流只接受 research_gap，零候选工作流只接受 replan_query。LLM 必须生成符合本码语义的决策、具体理由和任务；不能换码、吸收其他码的目标或合并其他工作流的任务。模型不能输出 trigger_codes、cause_event_ids、STOP、预算、coordinator_status 等程序字段；未声明字段直接拒绝。

`plan_supplement()` 返回 `SupplementPlanningResult`：brief（只替换 research_tasks）、最终 recovery_decisions、failure_events、harness_audit。Workflow 使用返回的决策，检查只读事实与门控一致，不再自行复制原决策替代 LLM 的 action / reason。

程序验收约束：

1. 只为获准决策中的点生成任务，每个获准点至少一个；拒绝空输出、漏点和越界任务。
2. language 必须在启用语言中；本版每个触发码内每点每语言最多一条；不同触发码下同一点、同语言的任务保持独立。
3. `ResearchTask.task_type` 必须显式保留，并与本工作流触发码匹配；不能只靠 task_id 或 description 标注类型。映射如下：

   | 触发场景 | task_type | RecoveryDecision.action |
   | --- | --- | --- |
   | 首轮检索（非补检） | literature_search | 不适用 |
   | evidence.missing | research_gap | research_gap |
   | review.missing_features | feature_supplement | research_gap |
   | coverage.no_match | replan_query | replan_query |

   现有 language_supplement 等名称不作为这三种触发工作流的输出；语言通过 language 字段选择。任务类型校验拒绝局部纠错操作。
4. description 必须非空；模型应说明补检目标、研究方向、所需证据以及与已有尝试的区别。
5. task_id / attempt 由程序规范为 T-R{轮次}-{COUNT|SEMANTIC|QUERY}-{该码点内序号} / 新轮次，并检查与历史任务无冲突。
6. Workflow 再次校验返回值，拒绝替换查新点或改写 Brief 的其他字段；同一触发码工作流输出任一不合法则该码不派发，其他码成功输出仍保留；跨码任务 ID 和决策绑定不能相互覆盖。

## 5. Coordinator 工作规范

可执行策略：[core/coordinator_policy.py](../backend/src/novelty_agent_framework/core/coordinator_policy.py)。共享提示词 [coordinator/supplement.md](../backend/src/novelty_agent_framework/prompts/coordinator/supplement.md) 版本 7 说明业务目标与最终输出，不承担状态转换授权。

- 数量不足：依据数量差额和已有覆盖规划补充研究，说明下一轮如何扩大有效覆盖。
- 语义不足：针对 Reviewer 的特征缺口、缺失方面、原因与建议方向，说明所需比较证据。
- 零候选：阅读实际查询与成功/失败记录，提出不同的概念层次、研究范围或研究角度。不能只重复“再查一次”。
- 同点多个码分别规划：各自回答对应缺口、明确任务目标；不得把其他码的任务合并进来或重复套用同一笼统描述。
- 只规划完整研究任务。具体检索词、SearchPlan 和数据库语法交给 Planner；执行交给 Researcher。
- 不改变查新点，不编造文献、DOI、URL、证据或新颖性结论；允许用 Reader 回读已授权材料辅助规划，不把读取、全文获取、换来源、请求重试安排成新查新任务。
- 不扩大语言范围、轮次或预算，不解除 STOP。不能用空列表表示缺口解决。

### 5.1 Harness 按触发码选择可执行策略

入口：[core/coordinator_harness.py](../backend/src/novelty_agent_framework/core/coordinator_harness.py)。`COORDINATOR_WORK_SPECS` 将已有触发码映射到不可变的 `CoordinatorWorkSpec`；`CoordinatorPlanningPolicy` 实现选项生成、解析、工具参数构造及状态转换。统一错误码目录仍是唯一的码说明文档。原三份 recovery 专项提示词已由程序策略替代，不再参与提示词加载与预检。

| trigger_code | 策略 | 材料选项优先排列 | 允许 action / task_type |
| --- | --- | --- | --- |
| evidence.missing | count_gap | 有效卡、查新点、历史任务 | research_gap / research_gap |
| review.missing_features | semantic_gap | Review、查新点、有效卡 | research_gap / feature_supplement |
| coverage.no_match | zero_candidates | 历史任务、查新点、触发决策 | replan_query / replan_query |

材料次序是程序呈现的优先次序，不强迫读取全部材料。三类策略都可读取本次范围内的论文及其他已索引记录。

一个工作流只接收一个触发码、选择该码的一份策略，并包含该码的所有获准 point_id。本轮最多三个独立工作流，当前依次运行，模型客户端及全局预算共享，但消息上下文、Reader 记录、局部额度、输出和失败状态隔离。程序 STOP 和本次 Reader / 模型失败不会被当成新的语义补检触发。审计保存策略定义、版本、SHA-256 和适用点；策略 Python 源码由现有启动源码快照记录。

状态与选择协议：

1. `material_review`：程序根据授权索引与剩余读取额度发放读取选项，以及本触发码对应的“结束材料核对并生成规划”选项。无材料索引时仍明确选择结束。
2. LLM 只返回 `{"option_id":"v1_0"}`。每轮请求使用 `response_format=json_schema`，枚举仅包含当前编号，`additionalProperties=false`，关闭原生 tools。
3. 选择材料后，程序填入 material_id、固定字段指针、实际分页位置和读取额度，执行 Reader；成功、失败或 EOF 都更新菜单并使旧编号失效。读完或失败的材料不自动重复读取；有剩余内容才出现继续读取选项。
4. 选择结束后进入 `draft`，菜单为空，恢复最终决策输出契约。此时 LLM 生成有业务含义的 reason、任务目标和描述，程序校验动作、类型、语言、查新点和读取引用。
5. 未结束时直接提交草稿、原生工具调用、额外参数、未知编号、旧编号和选项标签均拒绝，不执行工具；错误按既有 Harness 错误码记录，允许在剩余模型轮次内重新选择。
6. 探索预算耗尽时，程序可以强制转入一次禁用工具的收尾；这是显式运行时例外，审计记录原因，不由模型自行绕过阶段。

流程控制采用硬约束；研究方向和任务内容仍由 LLM 生成，不能用菜单编号替代实质规划。规划质量需要单独核对，不能仅凭选项合法就认定研究方向有效。

### 5.2 Coordinator 工具库与执行额度

目前只注册 `reader`，实现：[tools/coordinator_reader.py](../backend/src/novelty_agent_framework/tools/coordinator_reader.py)，通过已有 ToolCallHarness 串行执行。与 Researcher/Reviewer 的 Artifact Reader 共用 Harness/工具注册协议；针对业务记录索引使用独立适配器，不把业务材料伪装成 Artifact。

以下是程序填入 Reader 的参数，**不是 LLM 可以提交的参数**：material_id、content_pointer、char_start、max_chars。策略读取论文 /full_text 字段，其他材料读取索引指向的整条记录；每次最多 4000 字符，并裁剪至剩余累计字符额度。例如运行时解析一个论文读取选项得到：

```json
{"material_id": "M1", "content_pointer": "/full_text", "char_start": 0, "max_chars": 4000}
```

返回 read_id、material_id、point_id、文件路径/指针/哈希、实际字符范围、text、has_more。每次实际读取重新核对文件哈希和根目录范围；模型不能传任意路径、扩大索引范围或执行下载、搜索、写文件。

每个触发码工作流默认最多 6 次模型轮次（包含 1 次预留收尾）、4 次工具执行、累计 24000 个读取字符。选项选择和阶段转换也消耗模型轮次，纯阶段转换不计工具调用。超过探索额度时只保留一次禁用工具的决策收尾；全局模型预算仍由原客户端约束，不重置。Reader 失败以结构化错误返回，并移除该材料读取选项；模型可选择其他材料或结束，不自行修改参数、猜路径或重复请求。这属于本 Agent 内的执行控制，不产生新的补检触发码。非法最终输出、伪造引用或模型调用失败停止对应触发码工作流，保留其他码的合法结果。不同码不能引用彼此的 read_id，即使涉及同一篇论文。

提示词要求模型给出有意义的新方向，结构校验只能保证格式与范围，不能证明语义规划质量。真实 LLM 的效果仍需实验评估。

## 6. 派发、失败与审计

成功后，Workflow 把模型任务描述与系统事实（触发决策、数量差额、历史尝试）一起传给 Planner，生成新的 SearchPlan，不克隆旧计划。历史描述中的系统上下文会在再次汇总时剥离，防止递归膨胀。

同步 Coordinator 调用在线程中执行，保留当前运行上下文；未创建新的运行预算。本轮至少一个触发码产生合法任务后才增加 rounds，多个触发码一起派发仍只增加一轮；max_rounds 包含首轮。所有触发码均规划失败时保留错误和缺口、直接汇总已有结果；部分失败时保留失败决策并派发其他码的任务，不伪造确定性任务、不自动消耗一个空补检轮。

审计文件在 `<output_root>/<paper_id>/recovery/<run_id>/`：

- attempt-N-input.json：本次 Coordinator 的结构化输入、摘要及经核对的完整材料索引。
- round-N-gate.json：触发事实、待规划或停止决策、失败事件。
- round-N-coordinator.json：模型规划后的决策及完整新任务，或规划失败终态；harness.trigger_workflows 按码分别记录实际输入、可执行策略、工具调用/结果/错误、最终输出、调用计数和停止原因。policy 保存每轮菜单、被选编号、程序解析参数、读取结果、最终阶段及强制收尾原因。

下一轮检查时将上一轮决策收入 recovery_history。报告和 NoveltyRunResult 保留历史与当前决策；任务结果仍由现有持久化链保存。Debug 关闭也保存补检审计。门控和模型规划审计分文件，避免互相覆盖，并按 run_id 隔离同论文的多次运行。

## 7. 当前验收与后续边界

离线测试验证三类入口按码选择程序策略、实际请求只允许当前 option_id、非法选择无 I/O、提前提交被拒、Reader 分页/哈希/额度约束、预算收尾恢复最终契约、决策绑定、同点多码独立执行、历史卡保留与回路复查。这不等于真实模型语义质量已验收。

运行评估记录流程违规次数、有效规划/派发率、语义缺口针对性、跨码重复任务、模型轮次、工具次数、token、耗时以及补检后有效证据增量。有效证据增量为零不等于补检机制失败；仍需核对追加任务确实执行并保留真实结果。需要对照时使用相同输入、模型与总预算，分别报告执行约束和规划质量。

尚未完成：各节点完整现场纠错与纠错终态接入、全局预算统一读模型、Planner 单任务失败与整批派发的一致处理，以及跨节点完整的纠错展示。当前保留既有停止保护和预算约束，不能把补检工作流接通视为整个 P0-05 完成。
