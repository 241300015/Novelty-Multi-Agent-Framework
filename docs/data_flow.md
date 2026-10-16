# 后端数据链、索引传递与文献分类

核对日期：2026-10-16。依据当前工作区源码，以及本地模型运行
`run-c5157f89f54f4e4f9bb41fe853d0c087`。
[本次运行记录](experiments/20261016_validator_gate_a_local/analysis.json)。

本文区分已经实现的行为、实际发现的问题和本轮改动，不能把“产物已落盘”等同于“下游按索引重新读取”。

## 1. 当前完整数据链

```text
PDF → PaperProcessor（MinerU，失败/质量不足时转文本层/OCR）
    → PaperDocument → PaperInput → paper-input/ 文件
或直接读取已有 paper.json → PaperInput
    ↓
build_paper_digest(PaperInput) → Extractor → NoveltyPoints
    ↓
Coordinator.plan → NoveltyBrief / ResearchTasks
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

## 3. 改动前运行暴露的问题与当前处理状态

- [ ] **已有全文未在重新检索的结果中重新呈现。** NP-2 首轮按需取得四篇正文并落盘；
  补检普通检索返回的 bundle 仍主要包含本次生成的摘要，full_text_artifact_ids 为空。
  存储合并不会把 FULL_TEXT_ACQUIRED 降回 metadata_only，但本次工具返回使用新 bundle 的记录，
  模型看到的可用资源与磁盘已有资源不完全一致。按需获取分支能够重用已有正文。
- [x] **受控读取模式中不再由模型填写 source_record_id / artifact_id。** 改动前补检出现
  `src_649f84754254b648f4199871` 对应尾部的 `art_649f84754254b648f4199871` 请求；
  后者不在 Manifest。该文献实际正文制品为 `art_2fbba4a9b10ba3f6cb38332b`。
  新模式仅接受选项编号，由运行时填写工具返回的真实索引；旧自由工具模式仍保留，未全局迁移。
- [x] **受控读取模式建立强制的下一步选择。** 改动前， NP-1 已收到三份摘要的 `has_more=false`，
  仍按 A→B→C 重复读 16 次；第 17 次在 Harness 的 PRE_TOOL 次数检查中被拒绝。
  这里 `has_more=false` 只表示该摘要制品读完，不能解释为全文读完。新模式不再提供已读摘要，
  正文续读使用上一段的 char_end；旧模式仍按原配置运行。
- [x] **Card 修复保留文献分组，Builder 支持来源已确定的拆分。** 改动前，修复调用把可信读取降为 reader_texts 字符串列表，
  没有显式传递 Work / Artifact / 范围对应关系；同时旧约束禁止修复卡数超过拒绝卡数。
  现在传入完整 reader_results（含 Work / Artifact / 范围 / 原文），修复数量上限改为已观察到的
  来源作品数，仍由 Builder 核验。跨文献且每条引文来源唯一时可拆分；引文来源歧义仍拒绝。
- [ ] **跨阶段以对象传递为主。** Extractor、Planner、Researcher、Reviewer 与 synthesize
  之间仍存在大量 state/内存传递。持久化和模型按索引取材是两件事，不能把落盘视为迁移完成。
- [ ] **连续正文读取累积撑满模型上下文。** 新受控读取实验中，Reader 已按不同偏移顺序续读，
  但原文片段与历史对话持续保留；本地 32K 模型返回 400：输入 32845 tokens，加 4096 输出预算
  共请求 36941 tokens。避免重复 I/O 不等于上下文管理完成。后续需单独处理读取长度、
  对话保留方式与模型 token 预算之间的关系，本轮仅记录，不扩大为上下文系统重构。
- [ ] **报告端输入覆盖需单独核对。** Renderer 已经读文件，但并非读取所有阶段文件或所有
  文献命名空间；后续若扩充报告内容，需明确对应产物路径和缺失处理。

原始证据：本次 runtime 的 `tools/0001_database_search.json`、`0022_database_search.json`、
`0035_database_search.json`、`0047_reader.json`，以及模型调用 `0042`、`0043`。
这些编号是运行内编号，不是业务索引。完整运行位置见上方运行记录。

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

## 5. 本轮实现与使用范围

### 受控读取

配置项为 `researcher.harness.guided_reading`，默认 false，便于独立比较；
本轮[实验配置](experiments/20261016_guided_reading/profile.json)已设为 true。

```text
第一轮：Planner 原始 strict + medium + broad
    → 遍历本轮未读摘要；每篇摘要读完后选择获取正文（暂存队列）或不获取
    → 本轮摘要全部处理完：继续第二轮，或停止检索
第二轮：移除 strict，传入 medium + broad；重复摘要决策流程
第三轮：再移除 medium，只传入 broad；重复摘要决策流程
    → 剩余策略为空：Harness 自动停止检索，不调用空计划
停止检索 → 顺序阅读已选正文，逐段至末尾 → 提交最终 JSON
```

2026-10-17 修正：候选切换和停止检索是本地状态转换，不执行工具、不扣工具次数；仍消耗模型轮次。
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

当前试验覆盖 database_search → Reader 路线；不在菜单中开放 reference_search、web_search、browser。
`enable_evidence_checkpoint=true` 暂不能与该模式同时启用，配置会明确报错。
补检的指定读取/全文获取从其绑定材料开始，仍受到 RecoveryToolRegistry 的权限限制。

检索轮次采用 3→2→1 的原始策略子集递减；底层检索器仍自行处理编译、回退和结果。
首轮已包含全部档位，后两轮不保证出现新查询或新候选。菜单不能保证选文或引文语义正确。

2026-10-17 验证：非 live 回归测试 1319 passed、4 failed、6 deselected；4 项失败与此前基线一致
（3 项 Reviewer benchmark 缺少历史备份数据，1 项配置测试引用已移除的 `_min_interval`）。
新增覆盖三轮真实传参、缓存隔离、原计划不变、空计划/零命中/失败/重复候选终止、八篇摘要仅一次检索、
停止后全文续读、提前收尾拦截及补检范围约束。本次没有重跑真实模型实验。

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

### 验证记录

- 定向测试覆盖菜单阶段变化、拒绝重复/越权调用、索引由运行时填充、正文顺序续读、任务间状态隔离，
  以及真实来源拆卡、歧义拒绝、既有 Builder/工作流边界。
- 全量离线测试：1303 通过、4 失败、6 排除。四个失败与前次基线一致：三个引用已删除的备份文献，
  一个断言 ArxivWebSearchTool 已不存在的 `_min_interval` 属性。
- [历史失败轨迹回放](experiments/20261016_guided_reading/builder-replay.json)：上一轮 NP-2 的一张混合卡，
  拆为三张 Card / 三条 Evidence，Builder 无拒绝；三张均通过当前 Validator 的字段与落盘溯源核验。
- [本地 Qwen 全流程结果](experiments/20261016_guided_reading/analysis.json)：31 个阶段正常结束，
  73 次模型调用（72 成功、1 次上下文超限），13 次实际工具调用全部成功；其中 Reader 6 次，
  同一任务内重复读取区间为 0。已观察到摘要 → 获取全文 → 正文连续读取的选择链。
- 仍有 37 次模型工具选择因直接调用旧工具或使用无效菜单编号被拒绝，未进入实际工具执行；
  这些选择仍消耗模型轮次。运行启动后补充清理了 capability note 对旧工具的矛盾描述，
  清理后的提示由定向测试覆盖，尚未重新执行完整本地模型实验。
- 最终仍为 0 张有效 Card，Reviewer 没有实际审阅 Card。原因包括未召回候选、引文不匹配及
  上下文超限。因此本轮确认“约束实际读取”和“已溯源草稿可拆分”，不宣称证据链完整验收。
- 本轮重新提取出 4 个 NP，前次为 2 个，模型生成和检索输入发生变化，不作为严格 A/B 质量比较。
- 提示清理及补检绑定材料测试追加后，相关定向测试再次全部通过；上述全量测试统计为追加前结果。

## 6. 固定任务的纯选项实验（2026-10-16）

本地实验分支 `experiment/harness-choice-only-20261016` 增加 `researcher.harness.text_choices=true` 模式。
在此模式下，模型请求不包含原生工具定义，也没有 Available tools 清单；Harness 提供当前编号与动作描述，
模型返回 `{"option_id":"..."}` 或最终 ResearchFinishDraft。执行结果作为 Harness 观察进入普通消息，
内部仍记录真实工具调用及完整溯源；Builder/Validator 数据契约保持。

两项固定任务、相同检索计划、相同 16 篇候选与全文、同一个本地模型和 Validator 的三组对照：

| 组别 | 模型调用 | 无效选择 | 重复读取区间 | Validator 接受卡 / 提交卡 |
| --- | ---: | ---: | ---: | --- |
| 原版 | 37 | 0（另有 1 次执行参数错误） | 13 | 1/1 |
| 拆分菜单版（历史旧提示） | 44 | 34 | 0 | 0/0，N/A |
| 纯选项版 | 29 | 0 | 0 | 0/0，N/A |

接口约束改进没有带来有效卡产出提升。新增明确记录的问题：

- [ ] **上下文与输出预算仍未共同约束读取量。** 纯选项版 NP-1 顺序读完摘要与六段正文后，
  输入加输出预算超限；错误路径保留原文读取，但尚未生成可保留的卡片草稿。
- [x] **候选切换消耗数据库工具预算。** 历史纯选项版 NP-2 因反复获取已有候选触及预算；
  2026-10-17 已改为本地候选切换及至多三轮策略递减。此次修正尚未重跑真实模型对照，历史实验数字保持原样。
- [ ] **查新声明与来源引文仍可能被模型混淆。** NP-2 收尾把目标 NP 的 DSGNN 声明写入 quote；
  Builder 找不到对应的参考文献原文并正确拒绝。正确选择动作不等于正确组织最终证据。

完整条件、指标口径、逐任务失败与真实请求审计见
[固定任务实验报告](experiments/20261016_harness_comparison/README.md)。这是新的六次固定任务调用，
不是将先前不同 NP 的两次全流程运行拼成对照；也没有执行 Reviewer 来判断证据的语义充分性。

### 2026-10-17：修正后 C 组重跑

固定两项任务与同一文献集合，仅重跑 C 组。NP-2 通过一次检索、八次摘要阅读及本地候选切换生成两张
Validator 接受的卡；未获取正文。NP-1 把菜单说明当作 option_id，28 次均被拦截，零检索、零卡。
本次合计 48 次模型调用，实际工具 9 次，没有重复读取。候选切换扣预算的问题在 NP-2 未再出现，
但编号协议仍可能造成轮次空耗。没有运行 Reviewer，两张卡的语义质量仍有问题；也未触发第二/三轮或正文队列。
详见 [C 组重跑报告](experiments/20261017_harness_c_rerun/README.md)。

### 2026-10-17：NP-1 请求级 JSON Schema 试验

仅在实验脚本给非空菜单决策请求加入 `response_format=json_schema`，按本轮编号动态生成枚举；
保持提示、预算、固定语料不变，不引入本地推理库或私有参数。三次首轮请求探测均合法；完整 NP-1 的
18 次决策均合法，19 次模型调用产出 1 张 Validator 接受的卡。但该卡仍把待查新的 GSAERU 写入参考综述的
贡献描述，语义归属问题仍存在。仅阅读了八篇摘要，未触发正文或后两轮检索。
此约束只在 `--decision-schema` 实验开关下生效，尚未推广到生产 Harness 或验证云端兼容性。
详见 [NP-1 Schema 试验](experiments/20261017_harness_np1_schema/README.md)。
