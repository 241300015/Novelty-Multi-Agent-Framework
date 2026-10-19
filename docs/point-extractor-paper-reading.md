# PointExtractor 持久化全文阅读契约（P0-01 / Issue #20）

正式工作流默认使用 [Extractor 受控 Harness](extractor-controlled-harness.md)，
其中阅读选择、生成和审核分开计数。本文的索引与恢复契约适用于两种入口；
下文“兼容流程”描述显式关闭 Harness 后的旧生成路径。

## 索引与两个入口

PaperInput 是提取阶段的第一优先级，也是当前全文阅读的实际数据源。“阅读全文／补读分段”均指持久化 `PaperInput.full_text`；已有 PaperInput 时不重新读取 PDF、不重复执行 MinerU / OCR。PDF 仅在上游需要生成 PaperInput 时解析。快照缺失或损坏时报告对应失败，不用重新解析 PDF 偷换本次输入版本。

PDF 经现有解析器生成 PaperDocument → PaperInput 后，`persist_paper_input` 登记输入快照；直接 JSON/API 输入由 `persist_workflow_input` 使用同一登记函数。工作流在提取节点重新登记本次运行输入，返回该次不可变引用，不通过“最新输入”指针选择运行版本。

复用现有 `paper-input/content-list.json` 作为入口，其新增 `reading_index` 指向 `paper-input/snapshots/<snapshot_id>/content-list.json`。后者是同一内容索引的版本化实例；同目录 `paper.json` 为完整 PaperInput。根目录的 full.md、others/paper.json 是最新输入的兼容视图；历史读取只认快照，不读兼容视图。

快照内容索引最小契约如下，供 P0-06 后续接入其他业务产物：

| 字段 | 语义 |
| --- | --- |
| schema_version | 索引契约版本，当前 1 |
| run_id | 活跃 RuntimeArtifactManager 的运行 ID；独立解析/输入使用 input-UUID |
| paper_id | 目标论文身份 |
| artifacts[].artifact_type | 当前 paper_input |
| artifacts[].relative_path | 相对该索引文件目录，当前 paper.json |
| artifacts[].version / sha256 | 产物版本 1 与文件字节 SHA-256 |
| artifacts[].generated_stage / status | paper_parse 或 workflow_input；可读状态 ready |
| segments[] | segment_id、start、end、preview |

`PaperReadingReference` 冻结索引相对路径、索引 SHA-256、run_id、paper_id，workspace 为调用端根目录。运行 `runtime/<run_id>/manifest.json` 的 `paper_reading_indexes` 连接这些索引；不把目标论文伪装成 Work 或参考文献，不修改既有 references/list.json 与 subject_references/list.json 语义。此项不宣称 P0-06 的报告编译器已接入。

字符偏移采用**持久化 PaperInput.full_text 的 Python Unicode 字符**，0 起始、左闭右开，包含换行；不是 PDF 字节、页码或 OCR 坐标。每段最多 4000 字符，覆盖全文无间隙。快照哈希同时保护分段表。正文不裁剪、不拼接摘要替代正文；每次 read 都重开索引及产物并校验。移动归档后可以将引用 workspace 重定位到归档的论文目录，保留其余身份字段。

## 新进程恢复

```python
from pathlib import Path
from novelty_agent_framework.core.paper_reading import latest_reading_reference, PaperReader
from novelty_agent_framework.agents.point_extractor import build_paper_digest

reference = latest_reading_reference(Path("outputs/paper-id"))
reader = PaperReader(reference)
paper, index = reader.load()
digest = build_paper_digest(paper).model_copy(update={"paper_source": reference})
chunk = reader.read(index["segments"][-1]["segment_id"])
# agent.extract(digest, previous_brief=None, attempt=1)
```

历史恢复应使用历史 trace/运行 manifest 中冻结的引用，不跟随 latest 指针；新进程不需要前一进程的 PaperInput 对象。

## 兼容流程的阅读、生成与去重边界

- 阅读提示词 `extractor/read_paper.md`（版本 2），配置项 `point_extractor.prompt_names.read`。兼容流程在生成任何候选前，由 Extractor 本地阅读门控显式询问“是否需要阅读全文，或需要补读哪些分段”，必须先返回决策及非空理由。`needs_full_text=true` 要求全部分段读完，提前 finish 会被拒绝；`required_segments=[...]` 则由程序按清单逐段读取，清单须非空、无重复且来自当前索引。`needs_full_text=false` 保留逐段选择模式，至少实际读取一段才允许 finish，并保留未读范围。
- 阅读阶段最多 24 轮，范围决策、参数纠正和逐段读取均计入；没有纠错时，决策后至多读取 23 段、92000 正文字符。指定清单由程序执行读取，不额外调用模型。另有生成/去重/补核查共享 5 次调用预算，仍受运行全局预算限制。要求的全文或指定分段未读完时，即使预算耗尽也不允许生成查新点；选择性阅读已读一段后预算耗尽，可保留局部提取及覆盖缺口。
- 生成提示词 `extractor/extract_points.md`；去重仍为 `reviewer/review_points.md`；补核查由 coverage 配置指定。阅读与生成职责分开，全部已读正文和版本化来源传给生成、去重和补充调用。
- 已读段使用 `paper:<run_id>:<index_sha256>:full_text[start:end]` 来源；摘要/作者声明贡献使用相同前缀的字段名。未读取段、任意自由标签及伪造范围不能用于成果，触发 `model.output_reference`。候选和补充结果执行相同检查。
- 来源检查证明“确实读取过所指内容”，不证明该段在语义上支持 claim，也不证明贡献独立性。语义效果需要 P0-08-E 受控实验。
- 初始生成最多 3 次，少于 3 点时仅增量补充；重复的完全相同 claim 合并并保留来源和特征。语义改写不能凭字符串消重，仍由现有去重映射和保守守卫处理。
- 去重后从至少 3 点下降到少于 3 点时，最多一次补核查。补核查仅做来源校验和完全同 claim 合并，没有再次语义去重；不可宣称消除了全部语义重复。
- 生成结构或来源错误有界重试，保留之前的合法候选和错误记录；若始终没有合法候选则失败。去重 JSON 无法解析会终止节点并保留候选轨迹；不合法删除映射不能授权删除。补充失败保留已保留点。数量达到目标不等于全文覆盖或语义完整。
- 摘要直接调用（无 paper_source）保留兼容行为；正式工作流总是带引用。DemoPointExtractor 仍是规则示例，不代表模型全文阅读能力。

## 错误与可检查记录

继续使用 `schemas/failures.py` 和 `error-and-supplement-codes.md` 的集中码，不新增另一套码表。`extract_points.read` 负责本地处理：决策/参数错误给出反馈，最多追加一次纠正；缺文件、索引版本或身份错误、路径越界、哈希不符直接停止，不能退回内存正文掩盖错误。阅读预算耗尽时保留已读范围并标记未读段，不能声称全文覆盖。

成功和失败的 `point_extraction_trace.json` 均保存在论文工作目录和本次快照目录，启用运行记录时另写 runtime diagnostics。轨迹包含索引引用、实际正文/字符范围、未读段、错误码/节点/动作、模型可见输出、调用数、生成/去重记录和成功的 final_points。结构化事件暂以 trace 字典承载，不宣称所有旧 Extractor 错误路径均已转换成 FailureEvent。

`novelty-points.json` 的 source_locations 随成果持久化；历史结果也可在快照 trace 的 final_points 中恢复。未读段使 coverage_complete=false，工作流发出 point_coverage_unresolved；即便所有段读完也明确不作语义完整性认证。

`reading.scope_decision` 保存全文决策或补读清单及理由，`reading.generation_allowed` 记录前置阅读门控是否放行。这两个字段随成功／失败轨迹落盘。

## 验证

前置门控用例覆盖：两入口的新进程按补读清单恢复读取（PDF 入口在持久化后删除原 PDF，子进程禁止再次调用解析器）；显式全文与指定分段两条路径；有／无 PromptLibrary；未经范围决策直接给出候选、无效分段、空理由的拒绝；全文提前结束纠正；全文／补读清单未完成时预算耗尽禁止生成。历史 live 记录对应提示词版本 1，不能作为版本 2 前置门控已经通过真实模型验证的证据。

`tests/test_point_paper_reading.py` 包含真实生成 PDF 的 text_layer 解析及直接输入两入口、新进程重载逐段拼回全文、后段正文进入模型生成请求、实际位置引用、版本和文件失败、本地参数纠正、伪造引用拒绝、预算耗尽、运行 manifest 关联及无 runtime debug 时成功/失败轨迹落盘。模型为确定性替身；无真实模型质量提升或 OCR 质量结论。
