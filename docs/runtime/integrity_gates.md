# Validator 与完整性门控

当前执行顺序：

```text
Researcher
→ validate_evidence（Validator：Card 字段、归属、元数据与落盘溯源）
→ review_evidence（Reviewer：单卡核验与查新点汇总）
→ validate_synthesis_input（Gate A：Review 合法性）
→ check_final_evidence_sufficiency
→ 补检或 synthesize_report
→ validate_report_integrity（Gate B：报告与阶段结果的一致性）
→ persist_report
→ render_report
```

## Validator

`DefaultEvidenceValidator.validate` 需要 Cards、Evidence、Tasks、NoveltyPoints、
论文 ID 和 ReferenceStore。它检查字段格式、Card ID 唯一性、任务/查新点归属，
并沿 Card → Evidence → Work/Artifact 核对本地文件路径、文件存在性、SHA-256、
字符范围与引文。每条 Evidence 必须有完整的 char_start/char_end；每条 Card 来源必须有
`artifact <artifact_id> chars:<start>-<end>` 格式的位置，并与其绑定 Evidence 的制品、
字符范围和引文一致。每条 Evidence 也必须在 Card 来源中有对应项；任何缺失或不一致均拒绝。
即使两边位置一致，也必须通过文件哈希、边界及原文引文核验，不能共同指向错误位置后放行。
该生产检查不能通过 `require_source_location=False` 关闭；字段预检不代表完整合法性核验。
显示的标题、DOI、URL 与引文所绑定的本地 Work/SourceRecord 元数据比对；
填写 DOI 或 URL 却没有可核对的本地记录时拒绝，不因缺少记录而放行。
外部真实性核验可通过 `metadata_retrieval` 注入启用，默认仍执行本地检查。
它补齐原先缺少可调用 DOI/题录数据库的缺口；DOI 只是可选检索条件，无 DOI 时可按
标题、作者、年份检索。三来源独立返回统一契约，上层按同 DOI 合并缺失字段并保留冲突，
Validator 最终与绑定 Work/SourceRecord 核对。只有确认一致的外部记录能补足本次检查的
本地标识符空缺，不改写清单或覆盖已有值；所有文件/引文/位置检查仍必须通过。
`integrity.metadata_authenticity` 分别保留查询执行状态、比对结果和具体错误事件引用；
所有失败原因进入统一错误码系统及工作流错误审计，不用“未核验”概括已执行后的失败。
无匹配、缺字段、歧义、冲突和技术失败分别记录，码表见[集中目录](../error-and-supplement-codes.md#validator-元数据核对2026-10-27)；
规则与调用方式见[数据库工具说明](../database-providers.md#validator-的真实性核对)，
实施与验收范围见[待办清单](../后端代码审查与待办清单.md#8-validator)。
URL 允许来自该 Work 的落地页、全文地址或 URL 标识符；仅规范化协议、主机大小写、
默认端口和空路径，保留路径大小写、查询参数和片段，不联网追踪重定向。
Card 引文须对应绑定的 Evidence。外部元数据核验不替代对论文内容和引文语义的判断。

Validator 不再按 confidence/relevance 的高低拒绝 Card；两字段仍必须符合 0～1 的格式约束。
分数来源与职责说明见[置信度与相关性评分调研](../置信度与相关性评分调研.md)。

Validator **不按标题去重**；同一文献的不同 Card 可以保留。重复 ID 属于身份冲突，
会被拒绝。拒绝记录与接受的 Cards 在进入 Reviewer 前写入 `evidence-cards.json`。
不合法 Card 不参与评审和后续数量检查；溯源诊断保留在审计中。

`validate_fields` 仅供合成数据的字段测试，不构成生产流程的完整验证。
`validate_card_integrity` 是底层溯源检查；旧名 `validate_synthesis_input` 暂作兼容别名，
不再代表工作流的 Gate A。自定义 Validator 必须实现扩展后的输入契约和完整校验。

## Gate A

工作流节点沿用 `validate_synthesis_input` 名称，以兼容运行记录；实际调用
`validate_review_integrity`。它只处理 Reviews，不删除或改写 Validator 放行的 Cards。

检查包括：每个查新点恰有一份 Review、结构与状态合法、Card/Evidence/Work/feature
引用属于当前查新点、Reviewer 新增证据能追溯到真实读取记录与文件，以及已有的裁定约束。
例如 `not_novel` 必须有同一 Work 对全部声明特征的唯一、带引用的 supported 比较项。
这些检查不替代 Reviewer 的语义判断。

缺失、重复或不合法的查新点 Review 会被替换为 `insufficient_evidence`，
`incomplete_reason=technical_error`，不再保留正式 verdict，并附带结构化失败事件。
合法的语义证据不足状态保持原样。未知查新点的 Review 被拒绝并记录。

Reviewer 原始单卡和汇总分别保存在 `reviewer/card-reviews.json`、`reviewer/point-reviews.json`。
Gate A 先根据 Reviewer 选择分成候选池和弃用池，只校验候选及候选单卡局部成果。
弃用池 `reviewer/discarded.json` 只留审计；`reviewer/candidates.json` 保留候选原单卡与逐点 Gate 状态。
正式结果写入 `novelty-reviews.json`；拒绝的原输出和原因写入 `integrity/gate-a.json`。

新结果带程序生成的 `report_disposition`。被拒绝的结果明确 `gate_status=rejected`，
不能因缺少 summary_harness_state 被当成旧协议放行。原始完整账本留在原始文件，正式 Review
只携带候选卡的证据与读取记录；同一读取被候选卡共用时仍校验。非法比较引用由 Gate 拒绝，
不在分池过程中静默改写。单卡执行、点级采用、Gate 校验与磁盘版本检查是不同状态。

## Gate B 与运行状态

Gate B 仍检查报告的查新点覆盖、Review 字段一致性、Card 引用、报告来源快照一致性及部分单卡评审的溯源。
其检查不修改报告，目前失败仍不阻止报告持久化和渲染。

Validator、Gate A、Gate B 的审计分别记录在 `evidence_integrity`、
`synthesis_integrity`、`report_integrity`。节点正常执行与内容校验通过是两个不同状态；
运行 SUCCESS 不表示科学结论成立。文件写入等基础设施异常仍沿用工作流的失败处理。

报告编译另有 `report-inputs.json` 的版本门控，检查 run_id、文件哈希及依赖版本；
通过它不等于通过 Gate B。Gate B 审计写入 `integrity/gate-b.json`，包含所检查报告的
`model_dump_json()` UTF-8 字节 SHA-256，同时保留 state 和可选 runtime 记录。
完整报告显示审计结果并注明“仅审计，不阻断”。新索引 v2 登记池和 Gate 审计依赖，v1 继续只读支持。
下游来源、文件覆盖顺序及归档目录详见 [数据流与落盘目录](../data_flow.md#7-reviewer-下游与落盘目录2026-11-16-收敛实现)。
