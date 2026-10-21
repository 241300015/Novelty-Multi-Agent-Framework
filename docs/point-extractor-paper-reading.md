# PointExtractor 持久化全文阅读契约（P0-01 / Issue #20）

## 索引与两个入口

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

## 阅读、生成与去重边界

- 阅读提示词 `extractor/read_paper.md`，配置项 `point_extractor.prompt_names.read`；模型选择未读 segment_id，程序执行和校验读取。至少读一段才允许 finish。最多 24 次阅读决策（至多 96000 正文字符），每次成功读取另调用 `extractor/summarize_paper.md` 生成不超过 240 字符的笔记，阅读阶段最多 48 次模型调用；另有原有生成/去重/补核查共享 5 次调用预算，均受运行全局预算限制。
- 阅读选择仅接收至多 32 个段描述及最近 4 条笔记；正文仅在对应段的摘要调用中发送。生成提示词 `extractor/extract_points.md` 和补充请求接收有界笔记及版本化来源；去重 `reviewer/review_points.md` 不再附加阅读正文或笔记。完整正文仍保存在读取 trace 中供审计，不重复发送给模型。
- 正式 OpenAI-compatible 客户端对全文阅读链的每次调用强制执行精确 token 准入，包含输出预留；测量不可用时拒绝，不因 profile 默认关闭准入而绕过。固定投影仍超限时明确失败，不继续调用模型，也不自动截断来源或 claim。自定义测试客户端须自行实现相应准入能力。
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

## 验证

`tests/test_point_paper_reading.py` 包含真实生成 PDF 的 text_layer 解析及直接输入两入口、新进程重载逐段拼回全文、后段贡献通过笔记进入生成请求、实际位置引用、版本和文件失败、本地参数纠正、伪造引用拒绝、预算耗尽、运行 manifest 关联及无 runtime debug 时成功/失败轨迹落盘。`tests/test_review_fixes.py` 补充 24×4000 中文字符的确定性压力测试（字符计数替身，不是真实 tokenizer）。2026-10-21 本地 Qwen live 用 8330 字符论文验证后段 HELIOS-LATE 贡献及合法来源，10 次调用的准入 token 与 provider usage 一致；单例通过不代表语义质量提升或 OCR 质量结论。
