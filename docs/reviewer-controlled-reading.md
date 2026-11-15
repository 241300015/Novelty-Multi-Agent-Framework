# Reviewer 单卡受控阅读与证据契约

本页说明当前默认实现。点级交接见[汇总契约](reviewer-summary-handoff.md)，落盘和报告边界见[数据流](data_flow.md)。实验报告和运行产物留在本地，不随功能 PR 提交。

## 单卡阶段和契约

```text
assess_card → decide_reading ──跳过──→ draft → complete
                    │
                    └─ Reader → reading → decide_reading（还有可选材料及预算）
                                      └→ reassess_card → draft → complete
```

模型在初评和重评时选择 `usable / partially_usable / needs_verification / unusable`，
在阅读决策阶段选择当前菜单内的材料或结束阅读。它只返回当前调用、当前版本的 `option_id`。
程序构造 Artifact、偏移和读取长度，不接受模型直接提交工具参数。
菜单由每次调用的新对象持有，过期或其他调用的选项无效。

最终模型输出单卡选择草稿 `CardJudgmentDraft`，由 Harness 编译为兼容的 `ReviewerSupportDraft`，
包含解释、相关 Work、特征对应和补充材料建议；绑定判断菜单的规则见下文。
模型不填写 verdict、confidence、读取状态、read_id、引用坐标或增量证据对象。
程序组装 `ReviewerSupportReview(kind="card_support")`。其 `status=reviewed` 仅表示单卡流程完成，
即使 `support_status=needs_verification` 或全部特征 unknown，也不表示点级科学结论已确认。
点级 `NoveltyPointReview` 和 `ReviewerSummaryDraft` 的既有语义保持不变。

`review_card()` 默认使用新流程；`review(request)`、历史 cards 接口保留。
`controlled_reading=false` 显式启用旧单卡协议，供兼容和历史复现实验使用。
新旧单卡结果通过 `parse_card_review()` 进入工作流、汇总、checkpoint 和部分成果报告。

## 程序验证和模型判断

### 单卡模型输入与实验隔离（2026-11-16）

默认受控单卡路径保留 Card 的贡献概述、异同、引用关系、基线标记、相关度和置信度，
以及 Evidence 的解释与置信度，供 Reviewer 评审。Harness 仍负责身份选项映射；原有
读取坐标、凭据等运行字段投影不变。保留 Researcher 判断不表示认可其正确性。

2026-11-15 为对照实验加入的判断字段白名单，曾直接接入默认路径；按用户要求现已撤出
生产模块，移入 `scripts/run_reviewer_input_ablation.py`。baseline 使用默认输入，stripped
组在顺序实验调用期间临时安装过滤，退出或异常时恢复。原始请求、Reader 授权与账本不变。
PDF/HTML 和绑定判断的历史实验入口复用该实验过滤，保持其原有剔除输入条件；正常工作流
不导入或启用它。过滤不是生产配置项。

原实验记录保持原样；历史结果不代表恢复完整输入后的新运行结果。checkpoint 指纹不改写。

### 验证职责

| 事项 | 程序负责 | 仍由模型判断 |
| --- | --- | --- |
| 阅读阶段 | 合法动作、初评/阅读/重评/说明顺序、完成状态 | 是否值得继续读取 |
| 材料授权 | Card→Evidence→Work→namespace/Artifact、文件校验、已绑定版本 | 材料在科学问题上的价值 |
| 阅读事实 | 实际调用范围、返回范围、来源哈希、文本长度、读取记录 | 文本表达了什么 |
| 证据身份 | 成功非空读取立即登记确定性 ID、原文及归属；空 EOF 只登记读取记录 | 哪条已登记证据相关 |
| 特征引用 | ID 存在、同 Work、Card/point 绑定、非 unknown 项有引用 | 引文是否真正支持关系、作者/基线归属、等价与矛盾 |
| 单卡结果 | 完成阶段后才接受说明；模型不能写运行字段或新颖性字段 | 可用性、支撑理由、未知和局限 |

登记成功不是语义核验。自由文本说明的真实性、归属解释和推理范围仍不能由 ID/哈希校验证明。
模型生成的阅读声明不能写入正式阅读事实；正式事实只取程序账本。

### 绑定文献与特征的判断选项（2026-11-15）

受控单卡最终步骤改用独立 wire 契约，不改变既有持久化领域模型、旧单卡兼容路径或点级汇总协议。
`ReviewerJudgments` 为 Card 证据绑定的每个 Work 与每个目标技术特征提供四种有明确含义的判断选项：
明确披露、仅披露部分、明确不符、材料不足以判断。没有可引用原文的 Work 仅展示不足以判断。
选项在当前调用内绑定 Work、feature_id 和规范 relation，模型不能改写这些身份或关系字段。

模型最终只填写：

- `material_assessment`：参考材料能确认什么、还有哪些限制，不能复述目标论文贡献。
- `material_evidence_choices`：上述材料说明所依据的证据选项，可以为空。
- `feature_judgments`：按 Harness 的 `comparison_1` 等固定槽位填写对象，每个槽位选一个 `judgment_choice`，并选 `basis_choice`、
  `attribution_choice`、`evidence_choices`，解释 `reason`。
- `supplement_request`：补充材料建议或 null。

依据选项区分直接陈述、术语/机制等价、有限推断和证据不足。归属选项绑定参考 Work，
明确“作者自身方法”指参考文献作者，另有他人工作、对比基线、消融设置和不能确认。
未知判断只能使用不足依据；非未知判断不能选择不足依据，且必须引用同 Work 的已登记证据。
同一 Work/特征重复、遗漏、跨调用选项、跨 Work 证据/归属及直接填写旧身份字段均被拒绝。

严格 schema 根据实时账本生成：每个固定槽位只允许对应 Work/特征的判断，约束同 Work 证据与归属选项，unknown 分支允许空证据，
非 unknown 分支至少一个引用。读取阶段仍使用原 `option_id` 菜单；格式修复使用相同最终契约。
编译器回填 novelty_point_id、Work、feature_id、relation、basis_type 和 source_context。
领域 `highly_relevant_works` 保留作为已有接口：Harness 汇集模型选中的材料/特征引用，
复制或标注对应说明来组织文献关联条目，不新增相关性或新颖性判断。

没有技术特征的旧输入允许空判断对象，只保存材料说明；不会自行创造待比较特征。
旧报告和 checkpoint 继续解析原 `ReviewerSupportReview` / `ReviewerSupportDraft`。
新行为需要重新执行单卡评审；仅重渲染旧报告不会改变旧判断。

实际模型选择、当前菜单和绑定表均写入单卡审计事件；持久化结果继续使用原始领域身份。
选项限制解决的是任务对象、术语和记录结构，不能保证原文确实蕴含所选关系。

账本包含所有通过验证的本次读取，是否在结果中被引用是另一件事。模型拿到
`evidence_choice + work_choice + quote + material extent`，不承担真实 evidence_id 或 read_id/坐标的转抄。
选项由 Harness 在当前调用中注册和解析，持久化结果仍使用原始真实身份；详见[选择协议](reviewer-summary-handoff.md#证据身份由-harness-负责)。
原有 Evidence 是输入证据，不冒充本次 Reviewer 的实际读取。
摘要和正文的范围来自材料目录；文件存在、全文下载成功均不能创建读取记录。
`artifact_read_complete` 只针对具体 Artifact，要求程序从起点连续读取到目录长度且 Reader 报告 EOF；
不表示所有材料均已阅读，也不表示已理解或核验所有特征。

当前 Reviewer 只提供 Reader，本阶段不增加网络获取工具。相同裸 Artifact ID 若在授权目录中
对应多个 namespace，现有 Reader 接口无法消歧，新菜单不提供这种读取动作。

### Harness 提示词适配（2026-11-15）

受控单卡 `review_support` 更新为 v4，点级 `summarize_reviews` 更新为 v5。
两者的代码内 fallback 与版本化文件保持相同正文；自定义部署提示后仍追加当前强制契约。
判断菜单的关系与依据说明同步调整，选项身份、Schema、状态机、预算和证据校验规则不变。

- 单卡按初评、阅读决策、重评、最终选择分别说明职责。材料可用不等于特征已确认。
  缺关键机制且存在正文和预算时优先继续阅读；只使用实际菜单支持的顺序读取，不假设可以跳转章节或联网检索。
- 逐特征区分披露、部分披露、同条件下明确不符和无法判断。理由只是“未详细描述、无法确认”时选择 unknown，
  不能通过 grounded_inference 把缺少描述转成不符。检查关系、依据、理由是否一致，但不新增输出字段。
- 限定目标主张、参考文献事实及综述介绍的他人工作的归属；材料说明限定于已读范围。
- 点级发现判断与理由冲突时，在当前菜单选择定向复核或放弃。复核后仍不认可则放弃整张卡，
  不改写单卡判断后采用；原记录保留。点级仍以既有单卡判断为主，不默认读取原文。

适用范围：单卡新提示只影响受控阅读路径；旧 `review_card`、`review_evidence` 及候选证据审查提示保留旧契约。
点级汇总提示是共享的，来自新单卡和兼容单卡的后续汇总都会使用 v5。报告使用统一组装与 sources 快照，详见数据流。
重渲染旧数据不会更新评判；新单卡行为需重新评审，新总结行为需重新生成总结。
checkpoint 将总结提示纳入哈希校验，旧 checkpoint 不能在新提示下原地恢复，需显式建立新的总结尝试，不能改写历史指纹绕过检查。

## 预算、错误和并发

- 沿用 `max_steps / max_tool_calls / max_total_read_chars / card_timeout_seconds`，不扩大默认值。
- `max_steps` 是新路径的全部模型请求上限，包括无效选择、提前结束纠正和最多一次格式修复。
- 工具调用和请求字符数在实际动作前扣除；失败不退还。菜单选择本身不消耗工具预算。
- 工具预算用完仍可在剩余模型预算内重评和说明；模型预算或总时间不足则标记未完成，不能跳过重评。
- 失败/超时结果保留已登记证据、读取审计和阶段；不能因最后一步失败而抹除已经发生的读取。
- 阅读失败后可选择其他合法材料或结束并重评；本次调用不无限重试同一失败材料。
- 每张卡独享状态机、账本、计数器和工具注册表的纠错状态。底层 Reader 使用请求作用域。
- 延续现有汇总 checkpoint：失败时保存单卡结果，显式恢复只重试汇总、不重新阅读。
- 新增单卡配置不影响已冻结汇总的配置指纹；main 旧 checkpoint 不因新增这两个配置项失效。
- 本阶段没有新增进程崩溃后的单卡续读接口；外部取消仍传播，不转成伪造完成结果。

新配置 `support_prompt` 默认 `reviewer/review_support`；原 `prompt` 保留用于旧接口/旧单卡路径。
typed 和 legacy 配置组合根均传递这两项及 `controlled_reading`。


## 验证与限制

离线验证覆盖阶段选择、真实模型请求边界、严格 schema、绑定槽位、证据范围/哈希、格式修复、预算、取消和并发隔离。主入口：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -m 'not live' tests -ra
```

受控本地模型验收工具为 `scripts/run_reviewer_controlled_local.py`，只有显式 `--live` 才调用模型。`scripts/run_reviewer_input_ablation.py` 保留显式实验过滤，不由正式工作流导入。

选项合法、读取回执可验证及运行完成，不代表科学关系、方法归属或结论正确。真实模型尚未覆盖所有自主复核/弃用选择，不能用离线分支测试代替语义验收。Gate B 保持非阻断；Reader 累积上下文的处理见[待实施方案](reader-context-management-proposal.md)。
