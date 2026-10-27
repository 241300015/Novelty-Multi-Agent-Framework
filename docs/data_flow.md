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
Reviewer：单卡可用性初判 → 独立选择是否回读 → 回读后判断与效果评价
    → 单卡并行完成／NP 内屏障 → NP 汇总 → Reviews，落盘
    ↓
Gate A：Review 合法性与引用/新增读取证据核验
    ↓
数量检查、恢复决策 → 补检任务回到 Planner / Researcher，或继续汇总
    ↓
CoordinatorSynthesisHarness 接收上游对象 → 按 NP 并发 synthesize_point（综述正文）
    → 全点屏障 → 组装并落盘 report-synthesis/<run-key>.json
    → 绑定 Reviews 与执行事实 → NoveltyReport
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
| Reviews → synthesize | Harness 接收对象，按 NP 划分 Cards/Evidence/Review；模型只写当前点的综述 | 全点屏障后原子写 `report-synthesis/<run-key>.json`；不是按磁盘索引重载 |
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
给出当前允许的修复动作、修改范围和选项 ID。可定位字段的错误从首次修复起就使用
`option_id + updates`：模型只提交程序发放的字段编号及新值，由程序从失败原稿组装。
仅在 JSON 损坏或缺少可定位父结构时使用完整草稿重建，仍检查修改范围。
程序依次核验选择、字段改动范围与实际改动，再运行原 schema 与编译器检查。

若修复越界，或选择修复却原样返回（`planner.repair.no_progress`），程序拒绝该响应，
在总预算内最多再反馈一次。字段路径与替换/删除操作由程序固定，失败响应不成为新底本，
未授权字段保持不变，旧选项失效；纠正后仍无效即停止。有改动但编译仍失败也不能当作成功。

2026-10-27 讨论更新：上述为当前实现描述。用户对“无改动／重复错误后有限反馈并退出”
方案持保留意见，将后续调研；随后同意暂时采用。Researcher 现启用同阶段同错误码两次即收尾的临时策略，Planner／Coordinator 原局部检查保留。该机制不是已验证的长期通用原则。现有实现范围、具体例子和
Researcher 阶段修复方案见 [问题记录](issues/09_Researcher生成质量与定点复现.md#2026-10-27重复错误退出机制待重新审视)。

修复协议只在本任务内有效，异步任务分别持有状态。临时服务失败最多原请求重试一次，草稿最多修复一次，
越界或无进展共享一次协议纠正额度，全部受原总尝试上限（默认 3）约束；最终失败直接抛出，本批 Researcher 不派发。
普通规划保持原草稿响应格式，修复阶段使用包含当前选项枚举及字段更新类型（或完整重建草稿）的 JSON Schema；
不支持该请求的服务按调用错误停止，不静默改回自由输出。
Planner 没有工具读取阶段，因此采用独立的局部 Harness；没有迁入 Researcher 的工具循环。

这实现了确定的控制流程与修改边界，不能保证模型一定修复成功，也不把既有结构/词项规则当成全面语义核验。
除脚本化回归外，2026-10-18 首批用本机 `qwen2.5-7b-instruct` 完成 22 次真实调用，
13 个场景检查符合预期：两篇论文固定查新点的正常规划、补检、5 次受控草稿修复、
越界拒绝和两任务并发派发门控（当时策略为越界即终止）。错误在真实模型响应上显式注入，修复由真实模型完成；
成功批派发 2 项，失败批派发 0 项，峰值规划并发为 2。
原始响应、注入记录与运行轨迹保存在本地实验分支 `experiment/planner-live-20261018`，
不进入 main；未运行下游检索/评审，未测自然错误率或相对成本收益，结论不能外推到完整链路。

同日新增越界协议纠正验证：最终版本实际调用本机模型 23 次，7 个场景检查符合预期。
标量、数组元素、额外字段删除、概念集合重建四种受限修复通过；再次提交非法字段编号时按预期终止。
生产规划图中，成功批和失败批各调用 4 次，分别派发 2 项和 0 项，峰值规划并发均为 2。
此前探索另调用 15 次，发现一次概念重建返回空数组，已被确定性校验拦截；补清数组提交说明后复测通过。
这次失败样本及全部 38 次调用记录保留在本地分支 `experiment/planner-scope-correction-20261018`，
不合入或推送 main。上述结果仍是显式故障注入下的固定任务验证，不能保证所有模型都能修复成功。
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
├── report-synthesis/<run-key>.json
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

正式配置默认启用 `researcher.harness.guided_reading` 和 `text_choices`，
由 ResearchReadingPolicy 管理阶段与选项，并使用 `research/choice_only` 提示词。

```text
第一轮：Harness 自动执行 Planner 原始 strict + medium + broad，不询问模型
    → 遍历本轮未读摘要；每篇摘要读完后选择获取正文（暂存队列）或不获取
    → 无文献返回：在计划与预算允许时自动启动下一轮并记录通知
    → 有文献返回：处理摘要／元数据候选，再选择继续下一轮或停止检索
第二轮：移除 strict，传入 medium + broad；重复摘要决策流程
第三轮：再移除 medium，只传入 broad；重复摘要决策流程
    → 剩余策略为空：Harness 自动停止检索，不调用空计划
停止检索 → Harness 自动顺序阅读已选正文及续段 → 切换结果契约 → 生成最终 JSON
```

候选切换和停止检索是本地状态转换，不执行工具、不扣工具次数；仍消耗模型轮次。
首轮检索先于首次模型调用，通知和真实结果一起进入上下文；不伪造模型选择，也不占模型轮次。
它仍计入第一轮的工具额度及物理请求预算。后续只有本轮返回文献才开放继续/停止选项；
零返回自动推进获准轮次，失败记录独立保留，不改写为成功零命中。无摘要、重复命中均不等于无文献。
空计划不执行空查询；指定读取/全文获取的补检不插入发现检索，其固定操作直接执行并通知。
每轮只复制并筛选 Planner 的原始策略，不改写策略内容、不处理底层查询回退或候选排序。
数据库缓存键包含实际执行的计划，避免不同策略子集误命中旧结果。
失败、零命中、全部候选已读也会推进轮次；缺档计划跳过重复/空子集，普通任务至多三轮。
这里的一轮指一次 database_search 调用。模型只选择检索或停止，不选择数据库；Harness 注入计划、
配置和预算，database_search 内部遍历全部已启用数据库，每库自行编译 query、执行请求和重试。
启用 Springer 即将其纳入该工具的数据库集合。工具合并各库 ResearchBundle，保留逐库 source_statuses；
一库失败不丢弃其他库结果，部分失败/未执行结果不能当作完整缓存。全部来源尝试结束才推进轮次。
检索执行 ID 包含数据库和查新点身份，避免相同 query 在不同库发生记录覆盖。
摘要去重按已读 Artifact 句柄；正文获取失败时保留工具错误并继续其他候选，不伪造正文。
补检的指定读取/获取保持材料约束；重试/换库补检仅执行一次原冻结计划，不擅自缩减补检范围。
正常完成和受限收尾共用独立结果入口、`research/finalize` 提示与 `ResearchFinishDraft` schema；
阶段决定响应如何解析，不通过是否含 option_id 猜测阶段，不再发送空动作菜单。最终格式错误
进入既有一次格式修复。受限收尾不能据此宣称已读完全部正文。
菜单发放前检查注册表和工具／读取预算，过滤不可执行动作并记录原因；读取参数由 Harness
裁剪到剩余额度。必需操作不可用标记 partial，可选获取不可用保留限制并继续其他材料。
缓存命中同样推进 policy，但不算新物理读取或新证据。
Researcher 暂时启用重复决策错误限制：同阶段、同选项集合、同错误码第一次反馈，第二次退出
决策循环并尝试无工具收尾，任务为 partial。新阶段重新计数，零命中不计错误；通用 Harness
默认不启用。此规则标注为临时问题，阈值和纠错效果留待后续研究，不能代替阶段与契约一致性。
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

正式入口的默认配置（类型化工厂和 `load_config()` 兼容入口均保留这些设置）：

```json
{
  "researcher": {
    "harness": {"guided_reading": true, "text_choices": true},
    "prompt": "research/choice_only"
  }
}
```

需要原生工具循环时，显式将两个开关都设为 `false`，并将提示词改为
`research/native_tool_loop`。直接构造 `TaskResearcherConfig` 的兼容默认不变；
应用默认通过 `load_application_config()` 加载。

每轮请求都关闭原生 tools，Harness 根据当前菜单构造 JSON Schema：option_id 的 enum 只含当前编号，要求该字段并禁止额外字段。模型选择编号，运行时填入执行参数；模型负责最终语义草稿，来源绑定仍由 Builder 编译。

纯选项提示独立配置，不拼接旧工具目录和原生工具技能。菜单为空和预算收尾时恢复调用方的草稿输出契约；引文纠错使用自身契约，不残留决策枚举。模型服务必须支持请求中的 JSON Schema；客户端保留选项检查，不静默放宽不支持的请求。

阶段转换仍占模型轮次；候选切换和停止检索不占工具执行次数。普通检索按原始策略子集逐轮减少，补检继续遵守其冻结范围。受控模式不保证语义判断正确，语义错误的识别、纠正及复核仍属于纠错机制的独立职责。

错误记录与恢复建议见 [错误码与恢复动作](error-and-supplement-codes.md)，Validator / Gate A 的职责见 [完整性门控](runtime/integrity_gates.md)。

## 7. Reviewer：先接受初判，再决定是否回读

正式配置默认 `reviewer.guided_reading=true`。每次由 Harness 向模型提供一张 Card、已有
Evidence、查新点特征和完整授权材料目录。第一阶段不执行 Reader：模型仅凭手头资料
判断 Card 是否可用于后续比较，初判不表示原文已经回读或独立核验。

| 阶段 | Harness 发放的选择 | 选择后的行为 |
| --- | --- | --- |
| `assess_card` | `usable`／`partially_usable`／`needs_verification`／`unusable` | 登记模型初判，再进入阅读决策 |
| `decide_reading` | 不继续读；或选择一份可读正文 | 模型独立决定；可用性状态不自动触发或禁止读取 |
| `reading` | 继续下一段；或停止并保留未读范围 | 先尝试本地材料；无正文时先读摘要，缺文件时记录实际读取失败 |
| `decide_acquisition` | 在线获取当前绑定论文全文；或不获取 | 仅本地尝试后、正文不可用且来源有获取能力时出现；不重新检索 |
| `read_acquired` | 读取新获取的正文 | 刷新并核验授权材料目录，实际 Reader 读取后才能进入复判 |
| `reassess_card` | 同一组可用性枚举 | 保留初判，另行登记补读后的判断 |
| `assess_read_effect` | `helpful`／`no_new_information`／`unresolved` | 模型评价回读效果；纠正初判也可以有帮助 |
| `draft` | 菜单为空，输出 `ReviewerSupportDraft` | 提交前后判断理由、回读效果说明、特征比较与真实引用 |

“接受初判”只表示程序接受合法枚举，不表示程序认可语义正确。每轮模型只选择当前
`option_id`，JSON Schema 枚举和后置检查同时生效。编号绑定本次单卡执行和当前阶段，
旧编号、其他卡编号、原生工具调用、额外执行参数不能触发读取。跳过回读时直接进入 draft，
不虚构复判和回读效果。每次阶段选择仍占模型调用预算。

单卡最终草稿、汇总及其格式修复同样使用 `response_format=json_schema`，
并保留生成后的结构、引用与证据校验。单卡和汇总均提供固定 `feature_catalog`，
输出 `feature_id` 限制为目录内编号；没有特征时不能生成特征比较。
汇总 schema 显式表达 `status=reviewed` 时 verdict、非空 verdict_reason 和 confidence 必填，
`insufficient_evidence` 时不能给出 verdict，与运行时条件校验一致；程序不代填语义结论。

阅读策略位于 [reviewer_reading_policy.py](../backend/src/novelty_agent_framework/workflows/reviewer_reading_policy.py)，
复用通用 ToolCallHarness 的菜单协议和工具执行。Reader 仍校验 Work、材料版本、哈希和字符范围。
Reviewer 注册 `reader` 与 `database_search`。后者通过
[ReviewerDatabaseSearchTool](../backend/src/novelty_agent_framework/tools/reviewer_database_search.py)
复用数据库按需全文获取能力，只接受 Harness 注入的本地尝试记录和当前 Card 绑定的
research_reference 数据库来源句柄。没有 Reader 尝试、自由搜索、其他论文句柄均在执行前拒绝。
来源没有全文获取能力时不发放获取选项。现有正文必须实际可读才算缓存命中；Manifest 有记录
但文件缺失或损坏时允许一次获取尝试，存储层仍拒绝覆盖内容冲突的旧文件。

在线获取消耗原有工具次数，每次至少为后续 Reader 保留一个工具额度及可用字符预算。
同一卡的本轮获取结束后进入读取或复判，不循环重试。获取成功后重新从 Manifest 和文件构造
授权目录，仍执行 Work、版本和哈希约束；新增证据只能从实际 Reader 返回的文本登记。
获取失败、只返回摘要或新材料未通过授权校验时，保留原资料及失败事实，不伪造全文。
`harness_state.acquisition_history` 区分工具响应成功与实际获得可读授权正文，
`read_history.origin` 区分本地材料和本轮获取材料。初始用户输入保留原目录，后续状态提供刷新目录。
每次读取尝试在策略中预扣字符与工具额度；不重复发放已读区间，但文件缺失后成功获取的制品
允许重新读。`artifact_read_complete` 仅表示选中材料从头读至末尾，
不能扩展为整篇论文、全部版本或全部特征核验完成。

Reader 只负责定位、校验和返回内容，不接收模型填写的调用者身份。稳定 read_id 标识内容片段；
ToolCallHarness 在工具返回后登记独立 read_receipts，包含 caller role/run/invocation、call_id、
材料哈希与范围，随运行时工具轨迹归档。Researcher 生成 Evidence 时携带这些回执，
Reviewer 的本轮回执保存在 harness_state 中。相同片段跨调用读取保持同一内容 ID，但回执不同。
已有 Card 的 Evidence 与索引由 Harness 继承，不要求 Reviewer 回读或重复填写定位。
受控流程的模型输入、草稿 schema、读取返回投影及汇总投影不暴露 read_id、读取回执和字符定位字段；
单卡草稿不再接受 read_citations。旧原生调用路径的读取结果与判断草稿同样改用 Evidence，
仅读取请求继续使用原工具参数；当前受控流程的读取参数全部由 Harness 注入。
实际回读返回非空文本后，Harness 按当前 Card、授权材料及返回范围登记 ReviewEvidence，
把原文与 evidence_id 提供给模型。模型只选择判断所依据的 evidence_id；程序填写原文、
材料来源、范围和读取标识。登记读取内容不表示程序认定它支持某项技术判断。
Harness 通过 `evidence_options` 列出当前候选，并把引用数组的元素限制为同一组枚举。
单卡候选只含当前 Card 已提供的 Evidence 和本次成功回读后登记的 Evidence，回读后动态刷新。
预算收尾与格式/选项纠正沿用最新候选；越界编号不能通过后置校验，纠正仍越界时返回未完成。
汇总候选仅包含实际展示了非空引文的证据；空候选时引用数组必须为空，不允许自行填写编号。
没有回读就不登记新增证据；失败、EOF 不生成原文证据。历史结果仍可保留内部读取字段，
checkpoint 与完整性校验继续使用它们，汇总模型只接收过滤后的内容。

新单卡结果 `ReviewerSupportReview(kind="card_support")` 无新颖性 verdict。
`support_status` 保存最后一次模型选择，`harness_state` 保留初判、阅读决策、读取事实、
复判与回读效果。`status=reviewed` 表示协议完成，不表示 Card 一定可用。
中止时保留已登记状态，但 `status=insufficient_evidence` 和 `incomplete_reason` 标明运行未完成。
模型最终草稿不能写入或覆盖这些系统字段。

单卡提示词使用 [review_support.md](../backend/src/novelty_agent_framework/prompts/reviewer/review_support.md)，
单卡与汇总均加载 [判定方法 skill](../backend/src/novelty_agent_framework/skills/reviewer_information_adjudication/SKILL.md)。
汇总仍禁止工具调用；按原文和特征关系裁定，不把 Card 可用性映射为新颖性，也不按状态投票。
新协议向汇总携带已选回读引文及输入 Card 的原始 Evidence，每卡仍有 12,000 字符展示预算，
按剩余引文数分配展示额度并保留截断标记；未展示内容不支持裁定。

`reviewer.recover_summary_on_failure=true` 时，持久化成功的汇总 checkpoint 在模型超时或
未细分传输故障后最多自动恢复一次。初次与自动恢复共享原汇总截止时间和全局模型预算；
只重新汇总，不重新评审单卡。语义不足、格式／引用错误、预算耗尽和不可验证 checkpoint
不触发自动重采样。初次与恢复的独立产物均保留；显式恢复接口仍可用于符合原契约的恢复。

直接构造 `EvidenceReviewerConfig()` 仍保留原生工具基线，方便冻结实验比较；正式工厂的
类型化和兼容配置入口均显式开启新协议。`review_evidence.md`／`review_card.md` 保留给基线，
历史单卡结果可继续读取。新单卡与历史结果都能进入汇总、checkpoint、Gate B 的局部成果校验
和报告展示。离线契约测试不证明真实模型的初判质量或回读收益，效果仍需固定材料对照。

## 8. Coordinator 报告合成：按点并发、屏障和持久化

正式工作流调用 `Coordinator.asynthesize`，由
[CoordinatorSynthesisHarness](../backend/src/novelty_agent_framework/core/report_synthesis.py)
接受上游的查新点、Cards、Evidence 和 Reviews。先检查查新点唯一性、Review 完整覆盖、
Card 归属及 Review 绑定，再为每个查新点构造独立材料。全局模型调用预算与上下文记录继续生效。

每个材料包仅含该点的任务、Cards、原始 Evidence、Reviewer 结果和证据缺口，附带论文题名及摘要背景；
不发送其他查新点的结构化材料、整篇论文或内部读取定位。`synthesize_point` 不自行分配材料，
也不能选择或填写查新点身份、supporting/counter 分组或 Reviewer 裁定。

并发通过异步任务和 Semaphore 控制，上限来自 `workflow.max_concurrency`。
真实模型使用异步客户端；同步测试/兼容客户端在线程中调用，不阻塞事件循环。
每点独立记录 `synthesize_point` 子阶段及 point scope，完成顺序不影响最终顺序。

模型只输出 `PointNarrativeDraft(summary)`，最多 600 字，按“相关工作机制—有依据的共性和差异—
结论及限制”书写连贯综述，不能只重复 Reviewer 的 verdict/reason。结构化生成约束和后置
schema 检查同时生效；这些检查不等于对综述语义质量的自动验收。无材料或未完成裁定时，
仍遵守既有证据不足表达边界。

纠错参考 Planner，但不把报告修复塞进 SearchPlanDraft 协议：

- 首次结构错误即下发字段编号、操作和允许范围；模型选择修复/停止，只提交字段更新，程序组装。
- 合法的 summary 不因额外的身份或绑定字段错误而允许重写；额外字段只能删除。
- 非法 JSON 或无法定位对象时，仅允许重建综述字段，不能重建整份报告。
- 无实际改动、越界或失效选项会被拒绝；最多一次额外协议反馈，旧选项失效，授权范围不扩大。
- 每点总计最多 3 次模型调用。修改后的正文仍不符合契约时停止，不能靠重新开启修复预算循环。
- 明确输出截断、疑似字符串截断、无响应、超时及全局调用预算不足分别记录，不按普通字段错误无限重试。

Harness 等待所有点结束后形成屏障，按上游查新点顺序组装结果。点身份由程序填入，
Reviewer 的裁定、特征比较及相关文献关系直接继承；不让模型重新分组 Card，也不由程序根据词重合
猜测 supporting/counter。旧报告中的两个分组字段保留兼容，新合成路径不生成这类新分类。

屏障完成后，Harness 原子写入 `<output_root>/<paper_id>/report-synthesis/<run-key>.json`，
其中 run-key 为 run_id（独立调用无 run_id 时使用 standalone）的 SHA256 前 24 位。
文件包含本轮身份、分点输入、每点输出/失败、全部修复尝试及组装结果。
任何点失败时仍保留其他点的成功正文，但批次标记失败并阻断正式报告，不漏点发布。
取消时撤销并等待子任务，不把未完成屏障标记为完成。

全点成功后，工作流继续绑定检索执行事实、失败和恢复记录，经既有 Gate B 后，
由原持久化节点写 `report.json`，再交 Renderer。批次文件中的组装结果不是最终执行事实绑定后的报告。
同步 `synthesize` 入口保留给独立调用者，内部运行同一个异步 Harness；正式工作流使用异步入口。


### 技术门控与兼容入口的收尾

完整门控清单、失败行为及判断政策见 [Validator 与完整性门控](runtime/integrity_gates.md#2026-10-27-契约收尾与门控边界)。
断点恢复、Gate A 和报告局部成果共用读取一致性检查；新模型不参与读取定位登记。
Gate B 检查继承的相关文献 Card 引用，并将结果登记为 `report-integrity.json`；
默认报告显示真实检查状态，仍保留既有非阻断策略。
`report-synthesis/<run-key>.json` 纳入本次报告输入版本登记和附件；无 run_id 的独立诊断使用独立文件名。
历史整份响应复原与现行按点批次重放分开处理；历史 `read_citations`、报告分组及归档服务属于兼容读取，
不是当前 LLM 契约。同步适配器保留，生产异步接口声明为 `AsyncNoveltyCoordinator`。
