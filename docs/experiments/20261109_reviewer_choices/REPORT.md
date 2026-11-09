# Reviewer 证据选择协议 v2：实现与验证

先按用户要求提交上一阶段交接实现及实验记录，提交为 `b7f065c8`。
本页对应随后的证据身份设计修正，实现与测试已单独提交为 `052b558a`；实验记录另作一阶段提交。
原有 `docs/experiments/runtime/MF2033k6lC_2026-11-08/` 未纳入提交或修改。

## 设计与修改位置

| 文件 | 修改目的 |
| --- | --- |
| `core/reviewer_choices.py` | 调用内身份选项注册、精确解析、schema 枚举、继承判断复制与交接往返校验 |
| `workflows/reviewer_reading_policy.py` | 注册原始及成功读取的可引用证据；模型观察改成选项；动态生成最终严格契约 |
| `core/tool_call_harness.py` | 菜单耗尽后允许 policy 绑定最新最终契约，其他 policy 保持原行为 |
| `agents/reviewer_card.py` | 正常输出及格式修复经选项编译，真实 ID 仅由程序回填；保留本地运行和 strict 模式 |
| `agents/evidence_reviewer.py` | 点级选择草稿和复核请求编译；审计保存绑定；保持原引用、复核及裁定校验边界 |
| `prompts/reviewer/{review_support,summarize_reviews}.md` | 与内置提示同步，明确继承选择、修订字段、身份选项及职责 |
| `tests/test_reviewer_choices.py` | 直接使用真实模型输入中的选项测试新协议；不通过旧 ID 草稿适配器 |
| 严格输出、受控阅读、交接、checkpoint、点级和证据修复测试 | 保留既有语义断言并调整模型线上的选择契约 |
| `tests/reviewer_wire_fixtures.py` | 仅测试使用：把历史领域夹具转换成当前请求选项；生产不接受旧 ID 模型输出 |
| `scripts/audit_reviewer_summary_handoff.py` | 离线审计更新为 v2 模型投影，既有归档结果不覆盖 |
| `docs/reviewer-summary-handoff.md`、`docs/reviewer-controlled-reading.md` | 记录协议、例子、历史兼容范围和剩余限制 |

上述生产文件均位于 `backend/src/novelty_agent_framework/` 下。

以前，模型须抄写 `evidence_refs: ["rev_ev_..."]` 并完整重写继承比较；现在模型选择
`inherit_comparisons: ["j4c22cb89_1", "j4c22cb89_2", "j4c22cb89_3"]`，Harness 复制完整判断与真实证据关联。
直接评判或定向复核使用 `work_choice`、`card_choices`、`evidence_choices` / `evidence_selections`。
模型选项只是当前调用的能力范围，不能用来跳过 Card/Work/feature、已读、选中证据或内容支撑校验。

领域模型默认值、持久化真实身份、历史阅读账本、报告绑定和 Renderer 均未改动。
旧单卡材料可以在来源校验后重新生成点级总结；仅重渲染旧报告不会使用新的总结。
显式 `controlled_reading=false` 的历史单卡路径保留旧协议用于兼容；其输出进入新点级汇总时使用 v2 选择。
本次身份隔离保证针对默认受控单卡和点级汇总，不把旧路径说成已经迁移。

## 离线结果

最终扩展回归：**531 passed、3 skipped，17.00 秒**。
覆盖 Reviewer、严格 schema、选择协议、checkpoint、报告渲染与绑定、配置、主工作流、TaskResearcher，
以及共享 Harness、DatabaseSearch、Reader、Coordinator、Planner 的接入。
三个跳过均为 `test_reviewer_benchmark.py` 缺少旧实验原文 backups。

```bash
timeout 90 /home/lya3106643285/miniconda3/envs/Novelty/bin/python -m pytest \
  tests/test_reviewer*.py tests/test_novelty_point_reviewer.py tests/test_evidence_reviewer.py \
  tests/test_workflow_reviewer.py tests/test_renderer*.py tests/test_report_binding.py \
  tests/test_config_loader.py tests/test_configuration_startup.py tests/test_factory.py \
  tests/test_operation_prompt_config.py tests/test_workflow.py tests/test_task_researcher_workflow.py \
  tests/test_tool_call_harness.py tests/test_harness_decision_schema.py \
  tests/test_database_search_harness_integration.py tests/test_reader_tool_call_harness_integration.py \
  tests/test_coordinator_choice_harness.py tests/test_planner_repair_harness.py \
  -ra --tb=short -o faulthandler_timeout=25
```

单独的新选择契约与相关聚焦测试：77 passed。
测试检查真实发出的递归 strict schema，覆盖读取后新增枚举、跳过阅读、空引用、unknown、
非 unknown 的证据要求、同一契约格式修复、原模型/schema 不变、真实 ID 拒绝、拼错/过期/类型混用选项、
继承的理由和引用原样回填、定向复核、跨卡/Work/feature 范围及重复结构的格式修复。
`git diff --check` 通过。

## 本地模型复验

复用来源校验通过的冻结 NP-2 单卡结果；它来自样例论文 MF2033k6lC 的先前完整实验。
这是新的独立点级汇总实验，不修改或伪装恢复旧 checkpoint。
模型为本地 `qwen2.5-7b-instruct`，地址 `http://127.0.0.1:8000/v1`。
未重新检索、未重跑完整工作流、未生成或替换整篇查新报告。

所有尝试保留，详见 [results.json](results.json)：

| 尝试 | 模型调用 | 结果 |
| --- | ---: | --- |
| 预检 | 0 | 缺少本地 vLLM 凭证占位变量，请求发出前停止；随后使用 `LOCAL_VLLM_API_KEY=EMPTY` |
| `np2-choices-02` | 1 | 短选项合法，但同时继承并重写比较，结构重复被拒绝 |
| `np2-choices-03` | 1 | 结构重复仍被拒绝，说明仅调整内部校验顺序不足以解决模型对输出字段的混淆 |
| `np2-choices-04`（最终协议） | 1 | 使用继承列表，`revised_comparisons` 和 `additional_works` 为空；选择、引用和判断承接契约通过 |

最终协议将“继承”与“新增/修改”字段明确区分；结构歧义可走原有一次格式修复，
无效或过期选项仍直接拒绝，不进行模糊匹配。本次最终模型请求未用到格式修复。

最终调用耗时 **4.88 秒**；输入 4,015、输出 185、总计 4,200 tokens。
回填的三个 `FeatureComparison` 和相关文献条目与冻结单卡结果完全相同，六个真实证据引用完整保留。
模型输出只有继承选项，没有真实长证据 ID。默认交接原文字符数为 0；定向复核次数为 0。
旧 checkpoint 内容及来源快照未变。两次中间失败加最终一次，共三次本地模型调用，付费调用为零。

原始请求、输出、映射和审计保存在：
`outputs/reviewer-handoff-live-20261109/np2-choices-04/{calls,events,review,source,result}.json`。
原始大体积材料未纳入 Git；本目录保留摘要和文件哈希。

## 尚未验收的部分

- 最终模型仍输出 `novel / 0.9`，同时继承三个 `supported`，理由称技术特性“一致”。本次只确认选择和引用契约通过，**不认可该科学裁定**；没有改写权威裁定字段，也没有把这个结果写回旧报告。
- 单卡判断自身的内容归属和支持关系、unknown 与最终裁定的关系，仍需独立语义验收。
- 本次未触发真实定向复核或真实格式修复；这两条路径由离线测试验证。没有重跑新协议下的完整真实工作流或九场景本地模型验收。
- 未调用真实 OpenAI 后端、付费 API；严格 schema 的 OpenAI/本地传输形状由离线测试检查。没有运行全仓库和前端测试。
- 文献身份到展示编号的缺口、Researcher 上下文超限、通用上下文压缩均未处理。
- 当前同一调用内的短选项仍可能被模型选错；严格枚举与本地精确校验负责拒绝，不能代替内容判断。


## 后续完整真实工作流

随后按用户要求完成了[完整样例工作流测试](../20261109_reviewer_choices_full_live/REPORT.md)。
89 次本地调用、29 个阶段执行成功，短选项映射无误；但三次有效汇总均通过修订字段复写原比较，
另有一次重复判断在格式修复后仍失败，且科学裁定未通过验收。此结果补充而不覆盖上文独立回放记录。
