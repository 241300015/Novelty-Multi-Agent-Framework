# Issue #23 / P0-04 工作报告

日期：2026-10-26。仓库：`Novelty-Multi-Agent-Framework`。分支：`issue-23-p0-04-reviewer-recovery`。P0-03 集成基线：`dcd844d4249056eaabc8ec75ba3e002fe7067b7a`。本 Issue 提交：`a4a1fffc`（`fix: close reviewer recovery loop for issue #23`）。

## 结果

已完成 Reviewer 材料补全、单卡纠错、汇总恢复、Evidence 冲突隔离、变化卡复评和 Gate A 有界回流。Reviewer 只能为当前单卡已绑定的结构化数据库记录补取全文，不能执行新检索；获取结果按 SourceRecord 如实区分复用、成功、不支持、空内容和失败。技术、材料及预算错误不会被归类为 `semantic_evidence`，因此不能误触发 P0-05。

受影响范围的 231 项定向测试全部通过。全量测试收集 1707 项，1693 项通过、12 项跳过、2 项失败；两项失败与本次变更无关，分别来自当前 Starlette 路由对象形状和既有工作流测试缺失 report input 版本初始化。静态编译与差异空白检查通过。

## 实现

### Reviewer 受限全文补全

- 新增 `FullTextAcquisitionRequest`，全文补取不再构造虚假 SearchPlan。Researcher 的发现检索继续使用原请求契约。
- 新增模型侧仍名为 `database_search` 的 `ReviewerDatabaseSearchTool`。它要求单卡、非空 SourceRecord 列表、唯一任务绑定，并逐项验证 Evidence provenance、namespace、Work、SourceRecord、来源和 manifest；越权在任何 Provider I/O 前拒绝。
- 两个正式工厂入口构造一个底层 DatabaseSearchTool，由 Researcher 和 Reviewer 适配器共享来源实例及 ReferenceStore。测试验证 legacy 与 typed 配置路径行为一致。
- 逐记录返回 `reused_verified`、`acquired`、`unsupported`、`empty` 或 `failed`，附全文 Artifact 和 FailureEvent。部分成功保持已取得材料可用，全部失败时 Observation 明确失败。
- 复用正文前验证文件和 SHA。损坏正文会重新获取；只有 Provider 返回内容与已登记 Artifact SHA 完全一致时才允许原子修复，避免以新内容覆盖旧身份。

### Reader、Harness 与引用登记

- Reviewer 材料目录从只按 Work 放宽授权改为 namespace + Work + SourceRecord + Artifact 组合授权；没有 SourceRecord 的 Evidence 仅授权原始 Artifact。
- 单卡开始时目录用于模型提示，工具运行后重新读取 manifest 形成结束目录；ReviewEvidence 登记使用结束目录，并保留开始／结束目录审计。
- Harness 只把成功取得并验证的 `full_text_artifact_ids` 加入强制读取队列。Reader 必须返回非空文本才逐项清除；读取一个 Artifact 不会清除其他待读正文。
- 原有 read_id、文件 hash、字符范围、原文、Card、Work、SourceRecord、namespace 和版本校验继续作用于新取得正文。

### 单卡、汇总和版本恢复

- 单卡 Pydantic、引用、特征覆盖及裁定覆盖错误均把具体校验原因交给唯一一次无工具修复；修复后重新执行完整 schema、读取登记、引用和裁定校验。
- 单卡输入指纹覆盖请求、Evidence、材料目录及 hash/version、Prompt、schema、模型和有效配置。只有 `completed + card_verified + 指纹一致` 的结果可复用；材料、卡片、Evidence、Prompt、schema、模型或配置变化都会重评受影响卡。
- 汇总检查点在单卡终态后创建。Workflow 在初次汇总失败时自动恢复一次，恢复不调用工具、不重跑单卡，并与初次尝试共享检查点中的绝对截止时间及运行级模型费用账本。旧检查点没有共享截止时间时拒绝恢复。
- Workflow 在执行前按 Reviewer 签名识别 legacy 接口；现代 Reviewer 内部 `TypeError` 不再触发整批兼容重跑。

### Evidence 冲突和 Gate A 回流

- 原 Evidence 在字典化前按 ID 分组。完全相同内容确定性去重；同 ID 不同内容会生成冲突问题并移除歧义项，只隔离引用这些项的 Card，避免 last-write-wins。
- Gate A 自身也检查原 Evidence ID 缺失／歧义，并为拒绝项保留具体 FailureCode。
- 单卡结果落盘 `input_fingerprint`、`card_verified`、`reused` 和 `point_gate_verified`。汇总失败不删除有效单卡，但 Gate A 通过前不把 NP 结果标为可交付。
- Gate A 拒绝后构造包含 point、受影响 Card、输入指纹、错误码、原因、归属和剩余额度的回流记录。Workflow 最多执行一次无工具的受影响 NP 重汇总，然后重新运行完整 Gate A；第二次仍失败即保留技术终态和两次审计，不增加 ResearchTask 或 rounds。

## Issue 验收覆盖

| Issue #23 要求 | 实现证据 |
| --- | --- |
| 两个工厂为 Reviewer 注册 database_search，共享配置和产物库 | `config/factory.py` 两入口共享同一底层实例；`test_reviewer_config_injection.py` 覆盖 typed/legacy |
| Reviewer 获取已有候选全文，不虚构 SearchPlan、不执行新检索 | `FullTextAcquisitionRequest` 和 `ReviewerDatabaseSearchTool`；空 ID、跨卡、跨任务、错来源及 manifest 错绑均在测试中拒绝 |
| 缺全文获取后 Reader 回读并登记 | 结束目录刷新、Harness 逐 Artifact 强制读取及新增全文登记集成测试 |
| 真实传递全文失败 | per-record acquisition result 和 `FULLTEXT_UNSUPPORTED/EMPTY/FAILED`；全部失败 Observation 为 false |
| 单卡错误反馈、局部卡与汇总检查点保留 | 单次具体错误修复、card rows 持久化、自动 `recover_summary()` 和共享截止时间测试 |
| Evidence 冲突、Gate A 回流、变化卡复评 | 冲突隔离、二次 Gate、未变卡复用和 Evidence 变化重评测试 |
| 仅语义不足交 P0-05 | `technical_error`、`material_unavailable`、`budget_exhausted` 与 `semantic_evidence` 分离；工具或 Gate 失败不会伪装成语义不足 |

## 验证

在 `Novelty-Multi-Agent-Framework` 中使用 `/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/pytest`：

```sh
pytest -q tests/test_reviewer_fulltext_acquisition.py tests/test_on_demand_full_text.py tests/test_tool_call_harness.py tests/test_reviewer_config_injection.py tests/test_factory.py tests/test_reviewer_summary_checkpoint.py tests/test_validator_review_boundary.py tests/test_persistence.py tests/test_prompt_library.py tests/test_agent_prompts.py tests/test_evidence_reviewer.py tests/test_reviewer_evidence_repair.py tests/test_reviewer_verdict_coverage.py tests/test_workflow_reviewer.py
pytest -q
PYTHONPYCACHEPREFIX=/tmp/novelty-pyc python3 -m compileall -q backend/src tests
git diff --check
```

| 验证 | 结果 |
| --- | --- |
| 本 Issue 定向回归 | 231 passed |
| 全量回归 | 1707 collected；1693 passed、12 skipped、2 failed |
| Python 编译 | passed |
| Git whitespace 检查 | passed |

全量回归的两项失败：

1. `tests/test_api.py::test_only_current_task_api_is_started`：测试遍历 `app.routes` 时假设每项都有 `.path`，当前 Starlette 的 `_IncludedRouter` 不满足该假设。
2. `tests/test_workflow.py::test_six_tasks_six_plans_dispatches_all_tasks`：夹具直接使用 `run_id=run-plan-completeness` 落盘，但没有先初始化 report input 版本索引，触发既有 `ReportInputVersionError`。

两项与 Reviewer、全文补取、Gate A 和本次修改文件无关；未修改测试或生产代码以掩盖它们。

## 边界与剩余风险

- 本地没有执行真实 arXiv/外部 Provider 冒烟测试；自动测试用受控 Provider 覆盖已有全文、补取成功、部分成功、不支持、空内容、异常、损坏修复及并发复用。上线前应在配置了真实凭据和网络的环境补一次脱敏 acquisition → Reader trace。
- 本项保证结构、权限、材料完整性、可追溯性和有界恢复，不证明模型的新颖性语义判断必然正确；受控语义效果验证仍属于 P0-08-V。
- 外部 DOI 身份核验、Gate B 阻断和新增 ResearchTask 的语义补检仍按原范围分别留给对应任务，本项没有通过扩权绕过这些依赖。
- GitHub Issue #23 尚未关闭；实现提交已随 `issue-sdq` 分支发布。

原有未跟踪文件 `Novelty-Multi-Agent-Framework/docs/local-commits-review-report.md` 保留，未修改、未暂存、未提交。
