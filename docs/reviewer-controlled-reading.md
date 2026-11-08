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
