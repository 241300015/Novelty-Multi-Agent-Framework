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
其中，本地缺少 DOI 时的外部数据库核验尚未实现，等待允许使用的数据库接入；
当前拒绝表示无法完成核验，不证明文献或 DOI 虚假，详见[待办清单](../后端代码审查与待办清单.md#8-validator)。
URL 允许来自该 Work 的落地页、全文地址或 URL 标识符；仅规范化协议、主机大小写、
默认端口和空路径，保留路径大小写、查询参数和片段，不联网追踪重定向。
Card 引文须对应绑定的 Evidence。此处不进行联网元数据核验或语义真伪判定。

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

Gate A 将处理后的 Reviews、原始拒绝输出和原因写入 `novelty-reviews.json`，同时保留
单卡评审记录。Reviewer 汇总会继承新增证据对应的读取记录，供 Gate A 复核。

## Gate B 与运行状态

Gate B 仍检查报告的查新点覆盖、Review 字段一致性、Card 引用及部分单卡评审的溯源。
其检查不修改报告，目前失败仍不阻止报告持久化和渲染。

Validator、Gate A、Gate B 的审计分别记录在 `evidence_integrity`、
`synthesis_integrity`、`report_integrity`。节点正常执行与内容校验通过是两个不同状态；
运行 SUCCESS 不表示科学结论成立。文件写入等基础设施异常仍沿用工作流的失败处理。
