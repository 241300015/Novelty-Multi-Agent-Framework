# 后端数据链、索引传递与文献分类

本文描述当前源码中的数据通路、索引含义和实现限制。产物已落盘不代表下游按索引重新读取。

## 1. 当前完整数据链

```text
PDF → PaperProcessor（MinerU，失败/质量不足时转文本层/OCR）
    → PaperDocument → PaperInput → paper-input/ 文件
或直接读取已有 paper.json → PaperInput
    ↓
build_paper_digest(PaperInput) → Extractor → NoveltyPoints
    ↓
Coordinator.plan → NoveltyBrief / ResearchTasks → 首轮/补检共用任务合法性门控
    ↓
Planner 按任务并行 → SearchPlans → 完整性检查、落盘 → 分发屏障
    ↓
TaskResearchRequest（本任务的 NP、Task、Plan）→ Researcher / Harness
    ↓
database_search → Provider → Work / SourceRecord / Artifact
    → 原文文件 + references/list.json
    → 工具返回候选信息与来源、制品索引
    ↓
按需获取全文：source_record_id → 已有或新取得的正文 artifact_id
    ↓
Reader：artifact_id → 命名空间、Manifest、文件 → ReferenceReadResult
    → 原文片段和读取索引进入模型上下文，并进入工具执行轨迹
    ↓
模型输出 ResearchFinishDraft（语义说明、逐字引文；不自行填写来源 ID）
    ↓
EvidenceCardBuilder：quote → 可信读取 → Work / Artifact / 字符范围
    → Evidence + EvidenceCard → TaskResearchResult
    ↓
并行结果收集 → Validator → 接受/拒绝 Card，落盘
    ↓
Reviewer：单卡并行 → NP 内屏障 → NP 汇总 → Reviews，落盘
    ↓
Gate A：Review 合法性与引用/新增读取证据核验
    ↓
数量检查、恢复决策 → 补检任务回到 Planner / Researcher，或继续汇总
    ↓
Coordinator.synthesize → 组装、绑定 Reviews 与执行事实 → NoveltyReport
    ↓
Gate B → report.json → Renderer 读取工作区文件 → Markdown 报告
```

### 各边界实际传什么

| 边界 | 当前传递方式 | 主要落盘产物与限制 |
| --- | --- | --- |
| PDF → 工作流 | 解析为对象；PaperInput 入口从 JSON 加载对象 | `paper-input/full.md`、`content-list.json`、`others/paper.json`、`images/` |
| PaperInput → Extractor | 内存中的 PaperDigest，包含摘要和有界正文摘录 | 不是只传论文文件索引让模型自主读取 |
| Extractor → Coordinator.plan | 内存 NoveltyPoint 列表 | `novelty-points.json`；下游仍直接使用 state 对象 |
| Coordinator → Planner | 当前 NP、Task；Planner 制定检索策略 | ResearchTasks 随 brief/state 传递 |
| Planner → Researcher | 屏障检查任务与计划完整性后，构造 TaskResearchRequest | 分发前写 `retrieval-plans.json`；执行仍传对象，不是让 Researcher 重载文件 |
| 数据库工具 → Researcher | 候选元数据、`source_record_id`、`artifact_ids`、`full_text_artifact_ids` | Work/SourceRecord/Artifact 持久化在文献 Manifest |
| Reader → 模型 | 读取结果含原文 text、范围及身份字段 | 底层按文件索引读取；返回后正文片段进入内存和模型对话，不是纯索引通道 |
| Reader → Builder | 本次工具轨迹中成功、可信的 ReferenceReadResult | 模型草稿只有 quote；Builder 在读取结果中匹配并确定来源 |
| Researcher → Validator | TaskResearchResult 的 Evidence、Cards 在 state 收集；Validator 同时核验存储 | `research-runs/<NP>/<Task>/attempt-N.json`；Validator 在评审前写 `evidence-cards.json` |
| Validator → Reviewer | 当前 NP / Task / Cards / Evidence 对象 | 不重新读取 evidence-cards.json；回读原文仍经过 Reader |
| 单卡 Reviewer → NP 汇总 | 内存中的单卡结果、引文投影、登记的增量证据 | 有单卡记录和汇总 checkpoint；汇总保留新增证据对应的读取观察 |
| Reviewer → Gate A | Reviews、接受的 Cards、原始 Evidence 对象 | Gate A 写处理后的 `novelty-reviews.json`，保留拒绝原文和单卡记录 |
| Reviews → synthesize | 内存对象；组装代码绑定 Reviews、失败事件、恢复决策 | 并非统一按阶段产物路径重载 |
| report.json → Renderer | 按工作区路径重新读取 JSON 文件 | 已经是磁盘读取；不是整条后端都通过内存传递 |

Renderer 当前必读 paper.json、novelty-points.json、retrieval-plans.json、evidence-cards.json、report.json；
可选读取 references/list.json 和 candidate-audit.json。它不直接读取 novelty-reviews.json、
subject_references/list.json；Review 的正式字段经 report.json 进入渲染。

源码入口：[工作流](../backend/src/novelty_agent_framework/workflows/novelty.py)、
[Extractor 输入](../backend/src/novelty_agent_framework/agents/point_extractor.py)、
[Renderer 文件加载](../backend/src/novelty_agent_framework/tools/renderer.py)。

### Planner 内部的受控修复

首次模型只输出 SearchPlanDraft；任务/查新点身份、概念及策略编号、布尔表达式由编译器组装。
首轮与补检在 Coordinator → Planner 入口复用任务合法性门控。Planner 内部发现草稿错误后，
[PlannerRepairHarness](../backend/src/novelty_agent_framework/core/planner_repair.py) 按错误码及结构化字段位置
给出当前允许的修复动作、修改范围和选项 ID。模型一次响应选择修复/停止并提交完整草稿；
程序依次核验选择、字段改动范围，再运行原 schema 与编译器检查。

修复协议只在本任务内有效，异步任务分别持有状态。临时服务失败最多原请求重试一次，草稿最多修复一次，
两者仍受原总尝试上限约束；最终失败直接抛出，本批 Researcher 不派发。普通规划保持原草稿响应格式，
修复阶段使用包含当前选项枚举与草稿的 JSON Schema；不支持该请求的服务按调用错误停止，不静默改回自由输出。
Planner 没有工具读取阶段，因此采用独立的局部 Harness；没有迁入 Researcher 的工具循环。

这实现了确定的控制流程与修改边界，不能保证模型一定修复成功，也不把既有结构/词项规则当成全面语义核验。
除脚本化回归外，2026-10-18 已用本机 `qwen2.5-7b-instruct` 完成 22 次真实调用，
13 个场景检查符合预期：两篇论文固定查新点的正常规划、补检、5 次受控草稿修复、
越界拒绝和两任务并发派发门控。错误在真实模型响应上显式注入，修复由真实模型完成；
成功批派发 2 项，失败批派发 0 项，峰值规划并发为 2。
原始响应、注入记录与运行轨迹保存在本地实验分支 `experiment/planner-live-20261018`，
不进入 main；未运行下游检索/评审，未测自然错误率或相对成本收益，结论不能外推到完整链路。
具体码、动作边界与审计字段见[错误码与恢复机制](error-and-supplement-codes.md)。

## 2. 索引各自代表什么

| 索引 | 指向对象 | 不能替代什么 |
| --- | --- | --- |
| paper_id + 本次 output_root/run_id | 被查新论文及本次运行工作区 | paper_id 本身不能区别两次运行 |
| novelty_point_id + task_id + attempt | 查新点下的任务和轮次 | 不同 NP 可同时存在 T-1，不能只用 task_id 寻址 |
| work_id | 一篇文献的逻辑身份 | 不指定摘要/正文文件，也不指定获取渠道 |
| source_record_id | 文献在某个来源中的记录 | 用于按需获取全文，不能直接给 Reader 读文件 |
| namespace + artifact_id | 某份摘要、正文等制品 | 不能用 source_record_id 改前缀来构造 |
| Artifact.relative_path + sha256 | Manifest 内的文件位置与校验值 | 文件存在不等于 Manifest 索引有效 |
| read_id + 字符范围 | 对具体制品内容的一次确定性读取 | 不是文献 ID；同一文件、范围和哈希重复读取可得到相同 read_id |
| evidence_id | 绑定任务、NP、Work、Artifact、引文位置的证据 | Card 需通过它继续追溯，不是只凭显示标题 |
| card_id | 某任务中针对某篇文献形成的证据卡 | 不是全仓库唯一的一文一卡；生成 ID 包含论文、NP、Task、namespace 和 Work |
| Reviewer 的 review_evidence / reader_observations | 新增读取的引文登记与实际读取记录 | 汇总不能只传新增证据、丢掉用于复核的读取记录 |

索引链：

```text
Card.evidence_ids → Evidence → (namespace, artifact_id)
                            → Work / SourceRecord
                            → Manifest.relative_path → 原文文件
                            → locator.char_start / char_end → 引文
```

目前 Researcher 的 Reader 请求主要传裸 artifact_id，由工具依次查询 research 与 subject 两个
Manifest 推断 namespace。返回结果包含 namespace，但这不等于输入已经采用完整 ArtifactHandle。
若将来两个命名空间出现相同裸 ID，当前优先查 research 的行为需要明确处理。

## 3. 索引与数据传递边界

- **工具返回与磁盘资源可能不完全一致。** 普通检索 bundle 主要反映本次结果，不保证重新列出磁盘已有的全文；按需获取分支可重用已有正文。
- **受控模式的材料索引由运行时填写。** 模型只选择当前菜单的编号，不能自行拼写 source_record_id、artifact_id 或读取偏移。旧自由工具模式仍保留。
- **摘要与正文分别判定读取进度。** 摘要的 has_more=false 只表示该摘要读完；正文续读使用上一段的 char_end。受控模式不再提供已经读完的摘要。
- **修复输入保留文献分组。** 引文纠错传递完整 reader_results，包括 Work、Artifact、范围和原文；修复卡数不得超过已经观察到的来源作品数。Builder 重新核验，来源唯一的混合草稿可以按文献拆分，歧义来源仍拒绝。
- **跨阶段主要传递对象。** Extractor、Planner、Researcher、Reviewer 与 synthesize 仍大量通过 state/内存传递，尚未统一为阶段产物索引。
- **正文阅读会累积上下文。** 避免重复 I/O 不等于完成上下文管理；原文片段和历史对话持续保留时仍可能超过模型输入加输出窗口。
- **Renderer 的文件覆盖有限。** 扩充报告内容时需要明确新增产物路径、文献命名空间和缺失处理。

## 4. 磁盘文献分类：哪些是目录，哪些是字段

目前确实有分类，但物理目录主要按“用途/语料库”和“文献身份”组织：

```text
<本次 output_root>/<paper_id>/
├── paper-input/                  被查新论文
│   ├── full.md
│   ├── content-list.json
│   ├── images/
│   └── others/paper.json
├── subject_references/          被查新论文自身引用的文献语料
│   ├── bootstrap.json
│   ├── list.json
│   └── documents/<work_id>/<artifact_id>.<ext>
├── references/                  查新检索过程中发现的文献和材料
│   ├── list.json
│   └── documents/<work_id>/<artifact_id>.<ext>
├── research-runs/<NP>/<Task>/attempt-N.json
├── novelty-points.json
├── retrieval-plans.json
├── evidence-cards.json
├── novelty-reviews.json
├── recovery-round-N.json
├── report.json
├── report/<paper_id>-report.md
└── runtime/<run_id>/             模型/工具调用、阶段输入输出、诊断
```

以下分类保存在 Manifest 字段中，不对应独立的实体目录：

| 分类维度 | 字段及例子 | 注意事项 |
| --- | --- | --- |
| 文献类型 | Work.work_type：article、conference_paper、thesis、patent、standard 等 | 不按 article/、patent/ 建目录 |
| 出版状态 | Work.publication_stage：preprint、published 等 | 与文件格式、正文获取状态不同 |
| 来源渠道 | SourceRecord.source_id / source_kind：arxiv、structured_database、web_supplement 等 | 同一 Work 可有多个来源记录；网页补充材料不是新的物理 namespace |
| 获取状态 | SourceRecord.access_status：metadata_only、full_text_acquired 等 | 不等于摘要/全文角色，也不等于语义已核验 |
| 文件用途 | Artifact.role：abstract、full_text、extracted_text、metadata、supplement | 同一 work_id 目录中可同时放摘要和正文 |
| 当前制品是否完整 | Artifact.content_extent：full、partial、unknown | `role=abstract, content_extent=full` 只表示完整摘要，绝不表示完整论文 |
| 文件格式 | Artifact.media_type 与扩展名 | txt 既可能是摘要，也可能是抽取的正文，不能仅凭扩展名决定下一步 |

本次实际 `references/list.json` 有 16 个 Work、16 个 SourceRecord、20 个 Artifact，
其中 16 个 abstract、4 个 extracted_text；subject_references 的 Manifest 为空。
例如同一文献 `wrk_fca63c45c23c04e1596fdf4c` 下同时存在摘要
`art_bb44f53337fd5d2e29968dfb.txt` 和正文 `art_f98362daf37498d480e81432.txt`。

组织规则来自 [persistence.py](../backend/src/novelty_agent_framework/persistence.py)，
分类契约来自 [references.py](../backend/src/novelty_agent_framework/schemas/references.py)。
Reader 通过 Manifest 读取 role、路径与身份；不能从目录名或 ID 前缀推断摘要/正文。

## 5. 受控读取与来源拆卡

### 受控读取

配置项为 `researcher.harness.guided_reading`，默认 false；启用后由 ResearchReadingPolicy 管理阶段与选项。

```text
第一轮：Planner 原始 strict + medium + broad
    → 遍历本轮未读摘要；每篇摘要读完后选择获取正文（暂存队列）或不获取
    → 本轮摘要全部处理完：继续第二轮，或停止检索
第二轮：移除 strict，传入 medium + broad；重复摘要决策流程
第三轮：再移除 medium，只传入 broad；重复摘要决策流程
    → 剩余策略为空：Harness 自动停止检索，不调用空计划
停止检索 → 顺序阅读已选正文，逐段至末尾 → 提交最终 JSON
```

候选切换和停止检索是本地状态转换，不执行工具、不扣工具次数；仍消耗模型轮次。
每轮只复制并筛选 Planner 的原始策略，不改写策略内容、不处理底层查询回退或候选排序。
数据库缓存键包含实际执行的计划，避免不同策略子集误命中旧结果。
失败、零命中、全部候选已读也会推进轮次；缺档计划跳过重复/空子集，普通任务至多三轮。
这里的一轮指一次选定来源的检索工具调用，不代表一次物理数据库请求；底层仍可能执行多条查询和重试。
摘要去重按已读 Artifact 句柄；正文获取失败时保留工具错误并继续其他候选，不伪造正文。
补检的指定读取/获取保持材料约束；重试/换库补检仅执行一次原冻结计划，不擅自缩减补检范围。
正常路径须完成菜单阶段后提交 JSON；外层预算耗尽仍可强制收尾，不能据此宣称已读完全部正文。
工具调用只接收当前菜单的 option_id，真实 source_record_id、artifact_id 和偏移量由代码填充。
旧菜单编号、模型自行拼写的 Reader 调用会在 I/O 前拒绝。每个任务调用有独立菜单与已读记录。
执行轨迹仍保留实际 database_search / reader 调用及其观察，Builder 可以正常收集可信读取。

通用 Harness 只接收可注入的选项策略接口；摘要/全文阶段规则位于工作流的
[ResearchReadingPolicy](../backend/src/novelty_agent_framework/workflows/research_reading_policy.py)，
没有把工具本身绑定给某个 Agent。实际执行继续通过原工具注册表与补检权限检查。

当前菜单覆盖 database_search → Reader 路线；不在菜单中开放 reference_search、web_search、browser。
`enable_evidence_checkpoint=true` 暂不能与该模式同时启用，配置会明确报错。
补检的指定读取/全文获取从其绑定材料开始，仍受到 RecoveryToolRegistry 的权限限制。

检索轮次采用 3→2→1 的原始策略子集递减；底层检索器仍自行处理编译、回退和结果。
首轮已包含全部档位，后两轮不保证出现新查询或新候选。菜单不能保证选文或引文语义正确。

### 按来源拆分 Card

[EvidenceCardBuilder](../backend/src/novelty_agent_framework/tools/evidence_card_builder.py)
先沿用整卡引文交集判断；不能归属单篇论文，但每条引文都能唯一归属某篇论文时，按 namespace + Work 拆开。
一条引文同时命中多篇论文且不能消歧时，仍拒绝，不猜测来源。

拆出的 Card 分别使用自己的引文及解释；没有逐篇归属的 overlaps、differences、possible_baseline 清空，
贡献说明取本组引文的 interpretation，confidence 不超过原卡和本组引文的最低值。
relevance 暂沿用原卡，后续语义准确性仍需 Reviewer 判断。Evidence.provenance.builder_split 记录拆分来源，
warning 说明清空了哪些结论。原始模型草稿仍保存在模型调用记录中。

同一任务、同一来源作品的稳定 Card ID 规则保持；若草稿中另有同作品的 Card，仍沿用保留首张的规则。
这里的一文一卡是任务内的约束，不是跨 NP、跨任务的全局去重。

## 6. 纯选项请求契约

启用方式：

```json
{
  "researcher": {
    "harness": {"guided_reading": true, "text_choices": true},
    "prompt": "research/choice_only"
  }
}
```

每轮请求都关闭原生 tools，Harness 根据当前菜单构造 JSON Schema：option_id 的 enum 只含当前编号，要求该字段并禁止额外字段。模型选择编号，运行时填入执行参数；模型负责最终语义草稿，来源绑定仍由 Builder 编译。

纯选项提示独立配置，不拼接旧工具目录和原生工具技能。菜单为空和预算收尾时恢复调用方的草稿输出契约；引文纠错使用自身契约，不残留决策枚举。模型服务必须支持请求中的 JSON Schema；客户端保留选项检查，不静默放宽不支持的请求。

阶段转换仍占模型轮次；候选切换和停止检索不占工具执行次数。普通检索按原始策略子集逐轮减少，补检继续遵守其冻结范围。受控模式不保证语义判断正确，语义错误的识别、纠正及复核仍属于纠错机制的独立职责。

错误记录与恢复建议见 [错误码与恢复动作](error-and-supplement-codes.md)，Validator / Gate A 的职责见 [完整性门控](runtime/integrity_gates.md)。
