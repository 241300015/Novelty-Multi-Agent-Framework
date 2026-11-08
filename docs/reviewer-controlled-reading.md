# Reviewer 单卡受控阅读与证据账本

## 本阶段范围（2026-11-09）

开始时目录为 `Novelty-Multi-Agent-Framework-reviewer-rebuild`，分支为
`refactor/reviewer-rebuild-from-main`，HEAD 与本地 main 均为
`7ff63dd061db0470cd15462e85d20ae3af40c180`。原有未跟踪实验目录
`docs/experiments/runtime/MF2033k6lC_2026-11-08/` 保留；未切换分支或改动其他 worktree。初次实现未推送；后续按用户授权补测、提交并推送当前分支。

阅读 main 的架构说明、Reviewer、Reader、Harness、工作流、checkpoint、Gate 和相关测试后，
只读比较旧分支 `refactor/reviewer-worktree`（`f60436e2`）。旧分支相对 main 涉及 85 个文件，
不能整体合并。本次重新实现其有价值的调用内菜单状态机和即时证据登记思路，复用 main 的
Harness/Reader、失败分类、持久化、汇总和校验接口。没有移植在线全文获取、Researcher、Planner、
Coordinator 重构，没有增加审查 subagent，也没有调整点级 unknown 裁定。

实施顺序：分离单卡支撑性契约 → 建立调用内状态机和账本 → 接通现有消费边界 → 离线回归。

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

最终模型输出 `ReviewerSupportDraft`，包含解释、相关 Work、特征对应和补充材料建议，
没有 verdict、confidence、读取状态、read_id、引用坐标或增量证据对象。
程序组装 `ReviewerSupportReview(kind="card_support")`。其 `status=reviewed` 仅表示单卡流程完成，
即使 `support_status=needs_verification` 或全部特征 unknown，也不表示点级科学结论已确认。
点级 `NoveltyPointReview` 和 `ReviewerSummaryDraft` 的既有语义保持不变。

`review_card()` 默认使用新流程；`review(request)`、历史 cards 接口保留。
`controlled_reading=false` 显式启用旧单卡协议，供兼容和历史复现实验使用。
新旧单卡结果通过 `parse_card_review()` 进入工作流、汇总、checkpoint 和部分成果报告。

## 程序验证和模型判断

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

账本包含所有通过验证的本次读取，是否在结果中被引用是另一件事。模型拿到
`evidence_id + work_id + quote + material extent`，不承担 read_id/坐标到证据的转抄。
原有 Evidence 是输入证据，不冒充本次 Reviewer 的实际读取。
摘要和正文的范围来自材料目录；文件存在、全文下载成功均不能创建读取记录。
`artifact_read_complete` 只针对具体 Artifact，要求程序从起点连续读取到目录长度且 Reader 报告 EOF；
不表示所有材料均已阅读，也不表示已理解或核验所有特征。

main 当前 Reviewer 只提供 Reader，本阶段不增加网络获取工具。相同裸 Artifact ID 若在授权目录中
对应多个 namespace，现有 Reader 接口无法消歧，新菜单不提供这种读取动作。

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

## 离线与本地模型验收（2026-11-09 补测完成）

初次实施在沙箱中完成 394 项离线检查，但部分异步测试等待、本地模型无法连接。
用户授权沙箱外执行后，同一 `127.0.0.1:8000/v1/models` 正常返回本地模型，四个原超时节点全部通过：
Coordinator supplement、PDF workflow construction、database search harness、Validator provenance recovery。
对照确认这些阻塞与执行环境隔离有关；没有因此修改其他 Agent 的异步业务代码。

| 检查 | 最终结果 |
| --- | --- |
| 全量 `pytest -m 'not live'` | **2045 passed，3 skipped，8 deselected，74.30 秒** |
| 新增受控阅读测试文件 | 38 项通过，包含阶段、预算、故障、归属、并发和源数据闭包 |
| 本地模型最终九场景（run-7） | 全部运行契约检查通过；包含四卡并发、正文读取、材料缺失和预算中断 |
| 本地模型 checkpoint 恢复（run-6） | 首次注入发送前超时，持久化成功；只重试汇总，冻结事实保持不变 |
| 固定合成样例的语义关系（run-7） | **2/3 完全匹配**，不能宣称语义测试全部通过 |
| `git diff --check` | 通过 |

三个 skipped 来自 `test_reviewer_benchmark.py`：仓库缺少旧 Reviewer 实验原文 backups，
当前目录及旧分支未找到指定原文，没有用重新获取或合成的文本冒充旧归档。
八个 deselected 是其他 live 测试，涉及外部模型、检索、OCR、Extractor 等，不属于本次 Reviewer 验收；
没有启用付费模型。本次本地 Reviewer 实测通过专用脚本显式执行。

本地实测发现并修正了两个协议问题：菜单清空后模型继续返回 option_id；非 unknown 特征漏填引用。
现在菜单阶段使用选项 Schema，说明阶段使用 ReviewerSupportDraft Schema；说明 Schema 导出 main 已有的
“非 unknown 项必须有引用”要求，实际引用身份和归属仍由现有校验核对。
菜单明确显示 Artifact role 和 content extent，优先列出正文；完整摘要不被标成全文。
这些改动没有引入点级 unknown 裁定规则。

**仍存在明确的模型语义错误。** run-7 的正文原文明确写明使用邻居测量值的中位数，模型读到了该片段、
引用了真实登记的证据，却把 F1 标为 `contradicted`。这说明引用存在、来源合法、阶段完成都不能证明语义正确。
其他探索轮次也有遗漏和错误关系；全部保留在验收记录中，没有改标成通过或用程序替模型升级结论。
单卡支撑性和点级 unknown 规则仍须分别讨论。

成本记录：最终九场景 38 次模型请求，输入 105,152 / 输出 4,338 token，墙钟 66.092 秒；
checkpoint 场景 4 次真实请求，输入 10,165 / 输出 781 token，墙钟 18.498 秒。
包括所有探索、失败夹具和最终验收，共 **180 次成功本地请求，输入 471,754 / 输出 21,157 token**。
首次缺少本地占位 key 的 9 次客户端尝试未发出请求；注入的汇总故障也发生在发送前。
付费模型调用为 **0**，未计量本地 GPU/电力成本。探索轮与最终轮输入及菜单不同，不把调用次数变化当作严格性能对照。

脚本固定使用 `http://127.0.0.1:8000/v1` 的 `qwen2.5-7b-instruct`，无云端 fallback，
单次运行总上限 64 次请求，输出目录必须不存在。可在 Novelty 环境复现：

```bash
python -m pytest -m 'not live'
PYTHONPATH=backend/src:. python scripts/run_reviewer_controlled_local.py \
  --live --output outputs/reviewer-local-new-run
PYTHONPATH=backend/src:. python scripts/run_reviewer_controlled_local.py \
  --live --checkpoint-only --output outputs/reviewer-local-new-checkpoint
```

[完整验收说明](experiments/20261109_reviewer_controlled_local/README.md)和
[机器可读结果](experiments/20261109_reviewer_controlled_local/results.json)包含所有轮次的成功、失败及误判。
原始模型输入/输出在本地 `outputs/reviewer-controlled-local-20261109/`，不混入原有实验目录。

仍未覆盖：真实论文集上的语义质量、跨模型稳定性、在线全文获取、跨进程单卡续读及大规模并发压力。
合成案例和四卡并发不能替代这些验证。第三方若只接受 NoveltyPointReview 形状的单卡结果，
应采用 `parse_card_review()` 或显式选择兼容模式；仓库内相关消费边界已接通。

## 两个独立的后续验收样例

以下为用户提供的历史现象，本次记录为后续验收要求，不宣称已复现实验或修复最终报告。

1. **文献编号/关联在汇总后丢失**：旧实验单卡记录有文献—卡片关联，汇总后报告丢失文献编号。
   后续应沿 Card→Work/Evidence→汇总→报告检查身份传递与展示，并验证文献编号的可追溯性。
2. **unknown 被升级为结论**：新 main 实验中三个特征都是 unknown、没有特征证据引用，
   却出现 `partially_novel` 和 `0.8` 置信度。后续须独立讨论点级裁定和置信度规则。

补回文献编号不能把 unknown 升级为已确认结论。此次单卡契约已不允许新颖性结论和置信度，
但没有替用户预先决定点级 unknown 的最终处理方式，现有汇总仍存在该待验收风险。

## 变更文件

- 新增 `agents/reviewer_card.py`、`workflows/reviewer_reading_policy.py`、`core/reviewer_evidence.py`、
  `prompts/reviewer/review_support.md`：单卡编排、阶段状态、账本、模型指令。
- `agents/evidence_reviewer.py`、`agents/reviewer_checkpoint.py`、`workflows/novelty.py`：新入口、统一预算、新旧结果消费。
- `schemas/domain.py`、`schemas/__init__.py`：支撑性契约、历史结果解析及部分成果兼容。
- `core/tool_call_harness.py`：可选的调用内状态/结果投影钩子；没有改动 Researcher 默认策略。
- `core/integrity_gates.py`、`tools/renderer_sections.py`：支撑性局部成果校验和展示。
- `config/agents/reviewer.example.json`、`config/schemas.py`、`config/factory.py`、`config/loader.py`：新配置及两种组合根。
- 新增 `tests/test_reviewer_controlled_reading.py`；更新 Reviewer 配置/工作流测试，并为历史 native/read_citation
  测试显式指定兼容模式（`test_novelty_point_reviewer.py`、`test_reviewer_evidence_repair.py`、
  `test_reviewer_failure_classification.py`、`test_reviewer_summary_checkpoint.py`）。
- `scripts/run_reviewer_controlled_local.py` 和 `docs/experiments/20261109_reviewer_controlled_local/`：本地模型验收及完整结果。
- 本文、`docs/data_flow.md`、`config/README.md`：流程和验收边界。

以上 backend 相对路径均位于 `backend/src/novelty_agent_framework/`。

## 04e3aff6 后的严格输出与简洁结论改进（2026-11-09）

本轮开始时 HEAD 为 `04e3aff623db94de1a0ecab3f56b5bb56eefd4f8`，分支符合要求，
上述未跟踪实验目录保留。没有合并 main、推送、部署或改动其他工作树。

### 严格输出

静态检查及离线请求捕获确认：原 `ReviewerSupportDraft.model_json_schema()` 的顶层、
SupplementRequest 及 FeatureComparison 分支未把全部 properties 列入 required。
这不是已复现的线上 API 错误。
[OpenAI 官方契约](https://developers.openai.com/api/docs/guides/structured-outputs)
要求对象字段全部 required、additionalProperties 为 false，允许用 null 联合表达可空值。
当前客户端使用 Chat Completions，并原样传递 response_format，没有额外的 schema 修复层。

`reviewer_card.py` 仅深拷贝并规范化发送用 schema：递归处理定义、数组、联合分支，
保留 `$ref`、nullable、枚举、长度和证据约束，移除发送副本的 default 注解。
领域模型、默认值和旧数据读取方式不变。提示、诊断事件、最终请求及格式修复共用该副本；
阅读菜单仍由 Harness 使用 option_id 契约，菜单结束才恢复最终契约。
非 unknown 无 evidence_refs 或引用不在账本内仍会被原有引用门控拒绝。

离线检查覆盖 OpenAI/本地地址配置下实际组装的请求载荷，而非连接这两个接口。
上轮离线阶段未调用付费 API，也未重新运行本地模型；当时真实 OpenAI 接受情况、本地解码器实测以及
特定模型的 schema 子集兼容性均尚未验证。保留的 minLength/minItems 等约束也意味着
不能据此宣称支持官方文档列出的所有微调模型。

### 报告生成链路与设计选择

| 环节 | 当前实现与本轮处理 |
| --- | --- |
| Coordinator 正常提示 | `prompts/coordinator/synthesize.md` 要求每点简短综述，最多 600 字；保留 |
| Coordinator 无 PromptLibrary 路径 | `agents/coordinator.py` 内置提示同样要求简短 summary；保留 |
| 领域模型 | `ConclusionNarrativeDraft.summary` 的 600 字上限保留；不新增必填字段 |
| 组装及再次绑定 | `core/report_binding.py` 确定性复制 Reviewer 权威字段；材料不足时可替换 summary 为状态句；保留 |
| 工作流 | `workflows/novelty.py` 在总结失败时保留已有局部单卡结果；非法 Coordinator 报告直接报错，不存在另一个自动生成结论的模型 fallback |
| Renderer | 原来有有效 summary 就跳过比较 fallback；现在整体与逐篇两层分别组织，逐篇内容始终呈现 |
| 模板 | 仍用 concise.md，增加整体总结与逐点逐篇两个小节；default 模板不变 |

整体总结使用论文对象、既有来源/文献数量、Coordinator 原 summary、Reviewer 原裁定和限制。
缺少可用 summary 时仅回退到已有裁定理由，不推断检索零命中或新的总体新颖性。
逐篇按查新点和已有文献编号组织，使用已封存 `novelty-reviews.json.card_reviews`、
`report.json.partial_card_reviews`、点级 feature_comparisons / highly_relevant_works，
保留作者方法、相关工作、基线或消融归属。没有新增 Renderer 模型调用或另一套 Reviewer。

同一记录去重；没有已完成的单篇评审就明确说明，不将历史点级形状的单卡 verdict 升格为最终裁定。
比较引用只核对当前封存材料中可关联的 evidence_ids / review_evidence，不重新做语义审查。
缺少引用时展示为待确认的原记录；unknown 明确说明未提及不构成已证实差异。
Work 到编号缺失或不唯一时保留 work_id，不按题名猜测编号，也不回写身份链。

### 验证、示例与迁移边界

- 首轮 Reviewer 契约/工作流/客户端检查：103 passed；扩展离线回归：487 passed、3 skipped。
  最后补充的空列表、nullable 补检及缺失 evidence_refs 已包含于扩展回归。
  三项跳过均因旧 Reviewer 原文归档未随仓库分发。最后文字整理后重新运行严格契约与
  Renderer 四个文件：68 passed；`git diff --check` 通过。
- 扩展命令：`python -m pytest tests/test_reviewer*.py tests/test_novelty_point_reviewer.py tests/test_evidence_reviewer.py tests/test_renderer*.py tests/test_report*.py tests/test_workflow*.py tests/test_coordinator*.py tests/test_supplement_coordinator.py tests/test_model_profile.py tests/test_model_tool_calling.py tests/test_validator_review_boundary.py -m 'not live'`。
  沙箱内在 `test_report_resources.py` 的 Starlette/AnyIO TestClient 线程同步处停滞；
  自动批准后在沙箱外运行同组非 live 测试，15.03 秒完成。未改动异步业务代码。
- 多点多篇夹具位于 `tests/test_renderer_conclusion_details.py::seed_multi_workspace`。
  [修改前完整示例](examples/reviewer-concise/before.md)由参考提交的 Renderer 和模板生成；
  [修改后完整示例](examples/reviewer-concise/after.md)由当前 Renderer 生成，使用相同合成材料。
  例中查新点 1 原先只有一段 summary，现在还有文献[1]/[2]的对应与未知；
  查新点 2 展示文献[3]/[4]、缺少单篇评审和 work-unlinked 的关联缺口。
- 已通过版本门控的旧数据可直接重渲染获得两层结构、现存单卡/特征比较及缺口提示。
  旧数据没有评审、原文或引用时，重渲染不会补造，需要恢复评审或取得证据。
  修改已有自由文本总结需要重新生成 Coordinator summary；本轮没有扩大共享生成要求，
  因此其他报告模式的模型调用、预算及摘要长度不变。
- 没有版本记录的历史目录仍需按现有流程登记/封存；不绕过报告版本门控。
  全量仓库、前端及真实模型/检索测试未运行。编号链缺失和 unknown 被升级为最终结论
  仍需独立验收；本轮不修改这两项上游语义，也不增加独立审查 subagent。

本轮文件清单：

- 后端：`agents/reviewer_card.py`、`tools/renderer.py`、新增 `tools/renderer_conclusions.py`、
  `templates/markdown/concise.md`（均位于 `backend/src/novelty_agent_framework/`）。
- 测试：新增 `tests/test_reviewer_strict_schema.py`、`tests/test_renderer_conclusion_details.py`，
  更新 `tests/test_renderer_concise.py` 中原先“有 summary 就不显示比较”的旧期望。
- 说明与示例：`README.md`、本文、`docs/examples/reviewer-concise/before.md` 和 `after.md`。

## 严格输出修复后的九场景真实本地补测（2026-11-09）

用户随后选择运行现有九场景本地验收，明确材料为合成。使用当前未提交修复代码，
原脚本不改动，固定调用 `http://127.0.0.1:8000/v1` 的 `qwen2.5-7b-instruct`，
无云端回退，最多 64 次模型请求。沙箱无法连接端口，获自动批准后在沙箱外执行。
这是一轮真实本地模型调用，不是真实论文检索实验，也不是 OpenAI 后端验证。

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost PYTHONPATH=backend/src:. \
  python -u scripts/run_reviewer_controlled_local.py --live \
  --output outputs/reviewer-strict-live-20261109
```

| 项目 | 结果 |
| --- | --- |
| 九场景流程检查 | 全部通过；包括预算耗尽的预期停止 |
| 实际模型调用 | 38 次：30 次菜单决策、8 次最终草稿 |
| 严格请求检查 | 实际发出的全部 schema 对象递归满足 required / additionalProperties，引用定义可解析 |
| 返回对象检查 | 返回 JSON 的对象字段完整，最终 schema 与提示一致；非 unknown 的非空证据约束保留 |
| 传输/契约错误 | 未观察到；本轮未触发格式修复，不能据此宣称真实修复重试已验证 |
| 用量与耗时 | 输入 106,353 token，输出 4,812 token，墙钟 72.69 秒 |
| 付费模型调用 | 0；未计量本地 GPU/电力成本 |
| 带预设答案的语义场景 | 仅 1/3 完全匹配；不是人工金标质量评测 |

| 场景 | 流程 | 语义检查 |
| --- | --- | --- |
| available_input | 通过 | 失败：F1 中位数聚合被判为 contradicted，理由却是 F2 的固定阈值 |
| fulltext | 通过，确实读取正文并保留原文账本 | 失败：正文明确给出中位数和手动阈值，模型仍把 F1/F2 判为 unknown |
| unknown | 通过 | 三个特征均为 unknown，符合预设答案 |
| missing | 通过 | 缺失正文没有登记为已读；未设置完整语义答案 |
| budget | 通过 | 两次调用后按预算停止；未设置完整语义答案 |
| concurrent-0 至 concurrent-3 | 四场均通过 | 所有权和读取完整性通过；未设置完整语义答案 |

本轮支持“修复后的严格输出契约能被该本地后端接受”，不支持“模型判断已可靠”。
保持全部错误结果，不自动修正、不重跑直到得到正确答案，也不修改点级 unknown 裁定规则。

用 available_input / fulltext 两个真实单卡输出额外生成 default 和 concise 呈现样例，
模型调用增量为零；保留原始结果文件哈希，`conclusions=[]`，明确显示尚未形成点级结论。
样例没有伪造 Coordinator 总结或论文检索事实，不能作为完整报告生成链的真实模型验收。

- [机器可读补测结果](experiments/20261109_reviewer_strict_live/results.json)
- [完整运行结果](../outputs/reviewer-strict-live-20261109/summary.json)
- [发送 schema 与返回字段检查](../outputs/reviewer-strict-live-20261109/analysis.json)
- [available_input 简洁呈现](../outputs/reviewer-strict-live-20261109/rendering/local-available_input/report/local-available_input-concise.md)
- [fulltext 简洁呈现](../outputs/reviewer-strict-live-20261109/rendering/local-fulltext/report/local-fulltext-concise.md)

原始请求/响应保留在该输出目录的 `calls/`，源码指纹保留于 `source.json`。
本轮没有执行 checkpoint-only、真实格式修复、点级总结或 Coordinator 模型调用，
没有调用真实 OpenAI 接口，也没有修改生产代码、其他工作树或既有实验数据。

## 样例论文完整实验（2026-11-09）

用户要求改用样例论文，并明确授权向公共检索/元数据服务发送论文派生关键词、检索式和
参考文献题名/DOI 后，完成一次独立完整实验。采用 `examples/MF2033k6lC.pdf` 对应的
既有解析输入《面向大规模动态图的图神经网络优化机制研究》（81,079 字符、91 条参考文献），
复制解析输入及 subject-reference 初始化缓存，未重新执行 OCR。
查新点提取、Planner、公共检索、证据校验、Reviewer、Coordinator 和 Renderer 均重新运行。

输出目录为 `outputs/reviewer-sample-20261109/`，注册表只保留本地 Qwen，禁止云端回退。
配置为最多两轮、并发 4、运行级最多 210 次模型调用。保留配置预检及源码快照；原始输入、
旧实验目录和本轮使用的生产代码哈希在运行后复核未变。没有额外实现 Reviewer 或新裁定规则。

```bash
LOCAL_VLLM_API_KEY=local NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  PYTHONPATH=backend/src:. python -u outputs/reviewer-sample-20261109/run.py \
  --paper-json outputs/reviewer-sample-20261109/input/MF2033k6lC/paper-input/others/paper.json \
  --runs-root outputs/reviewer-sample-20261109/runs \
  --profile outputs/reviewer-sample-20261109/profile.json --isolated-config \
  --max-rounds 2 --max-concurrency 4 --max-model-calls 210 --max-physical-provider-requests 48
```

| 项目 | 实测结果 |
| --- | --- |
| 工作流 | SUCCESS，31 个阶段完成；耗时 478.55 秒 |
| 模型 | 97 次本地调用：96 成功、1 失败；付费模型调用 0 |
| 记录用量 | 输入 810,836 / 输出 9,525 token；失败请求无成功用量记录 |
| 公共服务请求 | 记录 25 次，HTTP 状态均为 200；不等于语义或身份验证均通过 |
| 有效材料 | 3 个查新点，最终 1 张有效证据卡、1 篇报告文献 |
| Reviewer | 两轮各完成一次单卡评审；17 次菜单请求、2 次严格最终草稿，另有 2 次点级总结调用 |
| 严格契约检查 | 两个最终请求均成功；实际 schema 和返回字段检查无错误，未触发格式修复 |
| 读取与证据闭包 | Reviewer 读取 11 次，均成功；同 Work 引用、原文字符范围、文件哈希和实际 Reader 回执机械检查通过 |
| 报告绑定与呈现 | 裁定及证据字段与 Reviewer 一致；default/concise 均生成，新版两层结论呈现通过 |

**运行完成不代表科学裁定成功。** NP-1 和 NP-3 均为 `material_unavailable`，NP-2 为
`technical_error`，三个点的 verdict 均为 null。NP-2 的两轮点级总结均未完成引用验证，
记录为 `model.output_reference`；单卡结果保留在局部成果中，没有升格为最终结论。
第二轮一个 Researcher 请求因上下文 34,599 token（消息 30,503 + 输出额度 4,096）超过
32,768 被本地服务拒绝，任务保留为 partial；这不是 Reviewer strict schema 错误。
配置预检也记录了未强制精确输入准入的 warning。本轮记录问题，没有改变其他节点实现。

同一份最终落盘材料另用 `04e3aff6` 的 Renderer/简洁模板生成修改前对照，不增加模型调用。
旧版查新结论只有三个无法裁定状态；新版增加整体总结，并在 NP-2 下展示文献[1]、
已完成单卡材料评审和三个特征比较，同时保持点级无法裁定。报告中的原始科学陈述没有由
Renderer 重写或独立核实。真实样稿还显示“相关工作/材料评审/已有相关性”三段重复，
应后续精简，未在冻结实验结果中静默改写。

- [机器可读实验与审计结果](experiments/20261109_reviewer_sample_live/results.json)
- [简洁报告](../outputs/reviewer-sample-20261109/runs/0001/MF2033k6lC/report/MF2033k6lC-concise.md)
- [完整报告](../outputs/reviewer-sample-20261109/runs/0001/MF2033k6lC/report/MF2033k6lC-report.md)
- [同材料旧版呈现](../outputs/reviewer-sample-20261109/before-concise.md)
- [严格请求/报告绑定审计](../outputs/reviewer-sample-20261109/contract-audit.json)

本轮未验证真实 OpenAI、真实格式修复重试、多个有效文献的实时报告质量或成功点级裁定下的
整体叙事质量。文献编号链与 unknown 升级问题仍不视为已解决。后续需要分别处理点级总结
引用错误、Researcher 上下文超限及逐篇说明去重，并补足有效比较材料。

### 待修复问题一：汇总原文裁剪后仍暴露不可引用的证据 ID

后续架构调整：本节保留原实验诊断，不重写历史结果。当前交接实现已改为单卡判断与选中引用，
不再默认展开账本；按需复核和验证边界见[点级交接契约](reviewer-summary-handoff.md)。
新实现已做离线数据回放，并完成[真实工作流及 NP-2 补充实验](experiments/20261109_reviewer_handoff_live/REPORT.md)：
默认交接不展开原文已验证，模型协议遵循和科学判断仍未通过验收。

状态：已定位并通过原始请求/响应离线重放确认，**尚未修复**。

触发位置是 `agents/evidence_reviewer.py::_compact_summary_rows()`：每张卡的汇总引文
预算固定为 12,000 **字符**，按顺序分配。本轮每个 Reader 片段为 8,000 字符，因此第一段
完整展示、第二段仅展示 4,000 字符，其余段落的 `quote` 为空并标记 `display_status=omitted`。
程序只把有展示文本的 ID 加入汇总引用白名单 `validated_ids`。

与此同时，传入模型的单卡 `review.highly_relevant_works[].evidence_ids` 和
`review.feature_comparisons[].evidence_refs` 保留原引用，省略条目自身也仍带 evidence_id。
这些 ID 对应真实已读材料，但其原文没有出现在本次汇总模型输入中；模型沿用 ID 后触发
`summarize_reviews()` 的引用门控，报错：

```text
summary cites evidence not verified in card stage or without a displayed quote
```

| 实际汇总调用 | 展示情况 | 模型引用了多少个无展示原文的 ID |
| --- | --- | --- |
| `0050_local-qwen2.5-7b.json` | 4 段：1 完整、1 部分、2 省略 | 1：`rev_ev_cc001ad2b89c35b3b1b632ff`，用于 F3 |
| `0096_local-qwen2.5-7b.json` | 7 段：1 完整、1 部分、5 省略 | 4，用于 F2/F3 及相关文献引用 |

两次原始输出都通过 `ReviewerSummaryDraft` 结构校验，并试图返回 `reviewed / novel`，
随后被上述引用门控拦截，最终仍为 `insufficient_evidence / technical_error`。
因此不能把模型草稿中的 novel 当作实验的最终结论。这里是汇总输入与引用授权之间的
不一致，不能归因为不存在的文献或 Reviewer strict schema 不兼容。

后续修复应让展示证据和可引用集合保持一致，按特征及已引用依据分配片段预算，避免仅因
顺序截断而丢失后续关键证据。证据不足时保留不确定性，不放宽引用门控或升级裁定。
验收至少覆盖：多个已引用长片段、全部/部分/未展示引用、汇总引用白名单与输入一致、
裁剪后无法支持的判断明确受限。部分片段是否足以支持原判断仍需语义核验。

### 待修复问题二：Researcher 未按输入加输出的总 token 做发送前准入

状态：服务端真实拒绝已确认，**尚未修复**。

发生于 NP-1 第二轮任务 `T-R2-COUNT-1`，实际调用记录为
`0084_local-qwen2.5-7b.json`。请求累计 47 条消息（system 1、user 24、assistant 22），
消息正文约 119,949 字符，其中包含多轮 Reader 原文返回；字符数不等于 token 数。
服务端给出的精确 token 计数为：

```text
输入消息                 30,503 tokens
请求的最大输出额度        4,096 tokens
合计                     34,599 tokens
模型上下文上限           32,768 tokens
超出                      1,831 tokens
```

输入本身未超限，但加上预留输出额度后超限，服务端返回 HTTP 400。
该次请求记录的 `context_admission` 为 null，配置预检也提示未强制精确输入准入。
任务最终保留为 partial，不能将工具曾经成功读取的材料等同于已完成有效证据构建。
完整工作流仍能结束并产出报告，但这不代表该任务已成功。

后续应在发送前计入输出预留，按实际 tokenizer 做容量检查，保留必要证据及句柄并压缩
历史观察；无法容纳时明确结束或缩减输入。单纯下调输出上限不能解决历史持续增长。
验收需覆盖：输入未超限但输入加输出超限、多轮长 Reader 结果累计、裁剪后的证据可追溯性，
以及本地服务拒绝后的 partial 状态和失败记录。

[精简诊断证据](experiments/20261109_reviewer_sample_live/diagnosis.json)保存原始调用文件哈希、
展示/省略的引用列表、越界引用和服务端错误，不复制整篇论文或全部模型上下文。
完整原始日志仍在本地实验输出目录，未随本次提交上传。
