# Issue #22 / P0-03 工作报告

日期：2026-10-23。仓库：`Novelty-Multi-Agent-Framework`。分支：`issue-22-p0-03-local-correction`。基线：`b8b2756b83b67a1879e3c40a87307568fe66b810`。

## 结果

已实现原任务内 Reader、换来源、结束重试、Builder 补正与 Validator 拒卡回流。纠错保留原计划、可信读取、有效卡和失败历史，不增加补检轮次。针对性验收 116 项通过；非 live 全量回归 1681 项通过，另有两项已在未修改基线复现的失败。代码已由提交 `dcd844d4` 纳入 `issue-sdq`，GitHub Issue 尚未关闭。

## 实现

- Reader：修复 null/畸形读取集合；新增 `reader.range.out_of_bounds`，起点越界最多一次复位补读；EOF 和完整性错误不盲目重试。阅读策略在有限补读完成前保留失败候选。
- Research：认证、授权、协议、资源错误仅在已配置且原任务允许的来源间换源一次；冻结恢复指令和全文获取不换源。任务结束只重试真实瞬时失败查询，保留成功查询，不重跑完整研究；新增材料进入读取、构建及验证链。
- Builder：整体 I/O 异常允许一次重建，其他整体异常保留局部成果并停止；逐卡补正携带具体拒绝原因。目标输入与参考原文分离，来源混淆修正后重新校验真实文件及引文位置。
- Validator：拒卡返回原任务，使用同一预算和可信读取重建，再运行完整验证。仅替换复核通过的受影响卡和 Evidence，其他有效卡保持不变。LangGraph 使用 Overwrite 避免追加 reducer 留下旧版本；依赖旧证据的评审失效。
- 审计及预算：新增 LocalSession 负责计数及持久化，业务决策仍在各责任节点。CorrectionRecord 记录发现、建议、执行、验证和终态，并保留关联对象及版本。内部工具调用也写入 Runtime 工具日志。原 FailureEvent 保留，已验证解决的错误不再次作为当前阻塞。
- 基线已移除了局部动作的补检路由，本项未重复建立该路由；旧冻结 recovery 请求不叠加新的结束重试额度。

模型总额度、补正额度、工具总额度、单工具额度和读取字符额度共同约束执行。新增 `max_correction_model_calls` 默认 3，支持设为 0，已接入配置加载及工厂。预算与已尝试对象按原任务身份落盘，恢复不重置；中断未完成记录标为停止。旧检查点没有可信预算快照时不自动授予额度。

## 验证与复现

从 `Novelty-Multi-Agent-Framework` 执行，使用现有虚拟环境 `/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/python`：

```sh
python -m pytest tests/test_local_correction_closure.py tests/test_card_recovery.py tests/test_config_loader.py tests/test_correction_trace.py tests/test_database_search_harness_integration.py tests/test_reader_namespace_chain.py tests/test_recovery_closure.py -o addopts='' -q --junitxml=../report/issue-22-p0-03/acceptance.xml
python -m pytest -m 'not live' -o addopts='' -q --junitxml=../report/issue-22-p0-03/final-regression.xml
```

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 针对性验收 | 116 passed | [acceptance.log](acceptance.log)、[acceptance.xml](acceptance.xml) |
| 最终非 live 回归 | 1681 passed、2 failed、3 skipped、9 deselected | [final-regression.log](final-regression.log)、[final-regression.xml](final-regression.xml) |
| 未修改基线复现 | 同样两项失败 | [baseline-failures.xml](baseline-failures.xml) |

两项既有失败为 `tests/test_api.py::test_only_current_task_api_is_started`（当前 FastAPI 环境的 `_IncludedRouter` 无 `.path`）及 `tests/test_workflow.py::test_six_tasks_six_plans_dispatches_all_tasks`（ReportInputVersionError，缺少报告输入版本初始化）。使用 git archive 提取上述基线到临时目录，以同一解释器复现，没有修改这两项无关测试或静默排除它们。

新增 `test_local_correction_closure.py` 共 26 个参数化用例，覆盖五类路径的成功与失败终态、真实文件与 SHA/位置核验、原有效卡保留、范围/预算/EOF 边界、中断恢复、无替代来源、成功查询不重放、重试材料完整验证，以及补检轮次不变。模型使用脚本响应，HTTP 使用模拟传输；未调用付费模型或外部线上服务。

HTTP 上限用例直接运行 ResilientHttpClient：每次逻辑请求最多 3 次物理尝试，共享硬上限为 4，实测首次 3 次、结束重试 1 次，总计 4 次。一般上限为原请求加一次结束重试的 `2H`，并进一步受共享 HTTP 硬上限约束。

## 落盘审计证据

[运行证据汇总](runtime-evidence.json) 汇集 31 条实际测试产生的 CorrectionRecord 索引、错误码、终态、次数、任务归属和预算前后值。详细事件在 `acceptance-artifacts-final/<测试目录>/audit/paper-1/runtime/<run_id>/corrections/`，关联工具日志在同级 `tools/`，预算在 `local_correction_budgets/`。保留事件序列，而非只记录最终结论。

| 路径 | 测试目录前缀（同名前缀下编号区分参数） |
| --- | --- |
| Reader 范围修正/预算停止 | `test_reader_range_correction_i` |
| 换来源成功/仍失败 | `test_source_change_preserves_o` |
| 原失败请求重试成功/仍失败 | `test_task_end_retry_only_repla` |
| Builder 来源混淆成功/仍拒绝 | `test_source_confusion_has_real` |
| Validator 重建复核成功/失败 | `test_validator_round_trip_pres` |

例如来源混淆成功记录包含 `builder.quote.from_target_input`，状态序列为 detected → advised → executing → verification_pending → verification_passed；附实际参考 artifact SHA、恢复的 card/evidence ID，模型用量由 3 增至 4、补正额度用量由 0 增至 1。失败变体保留拒绝和原有效卡，不以调用完成冒充验证成功。

## 边界与交接

外部 DOI 身份核验未实现，继续作为独立依赖；确定性验证只证明本地合法性与可追溯性，不证明贡献归属语义正确。Reviewer 恢复交接 P0-04，最终证据缺口与剩余失败交接 P0-05，真实模型语义效果实验交接 P0-08-R。本次未扩大这些依赖的实现范围。

错误码唯一目录同步为 149 个码（27 个原分类、122 个具体原因），恢复动作仍为 13 个。原有未跟踪文件 `Novelty-Multi-Agent-Framework/docs/local-commits-review-report.md` 保留，未纳入本 issue 修改。
