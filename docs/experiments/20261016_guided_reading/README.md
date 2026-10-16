# Harness 受控读取与 Builder 拆分实验

日期：2026-10-16。环境：Novelty；模型：本地 `qwen2.5-7b-instruct`，
地址 `http://127.0.0.1:8000/v1`。论文仍使用 `MF2033k6lC` 的现有 PaperInput。

## 本轮结论

- **读取约束生效**：模型选择菜单，Harness 填入可信工具参数。6 次实际读取均成功，
  同一任务内没有重复的读取区间；摘要之后成功获取全文，并顺序读取正文。
- **拆分回放成功**：上次一张卡混入三篇文献的真实引文，新 Builder 拆成三张卡和三条 Evidence，
  三张卡均通过当前 Validator。来源歧义、伪造引文仍被拒绝。
- **尚未完成证据链验收**：本轮最后依然是 0 张有效 Card。一个 Researcher 累积正文后超过
  32K 模型上下文，另有无候选、模型未读取便编写引文等问题。工作流正常退出、生成报告不等于科研结果有效。

## 实验入口

使用现有 `scripts/run_full_workflow_live.py`，经标准配置工厂运行，未新建绕过主线的执行入口。

```bash
LOCAL_VLLM_API_KEY=local NOVELTY_EXPERIMENT_PROFILE= \
  /home/lya3106643285/miniconda3/envs/Novelty/bin/python -u scripts/run_full_workflow_live.py \
  --paper-json outputs/MF2033k6lC/paper-input/others/paper.json \
  --profile docs/experiments/20261016_guided_reading/profile.json \
  --isolated-config --runs-root outputs/guided-reading-20261016/runs
```

运行路径：`outputs/guided-reading-20261016/runs/0001`。
启动时源码、输入、有效配置与校验记录位于该目录 `startup/`。
模型/工具原始记录在论文目录 `runtime/run-46029135375c43c99bac5709b69f3ce9/`。

## 数据与限制

| 项目 | 结果 |
| --- | --- |
| 工作流阶段 | 31 个 SUCCESS |
| 模型调用 | 72 成功，1 失败（上下文超限） |
| 实际工具执行 | 13 次，全部成功；7 次数据库工具，6 次 Reader |
| Reader | 1 次摘要、5 段正文；同任务同区间重复 0 次 |
| 正文起点 | 0、16000、32000、48000、64000 |
| 无效模型选择 | 37 次在实际 I/O 前拒绝，仍消耗模型轮次 |
| 最终有效 Card | 0 |
| Reviewer 实际模型调用 | 0 |

本轮 NP 数量为 4，前次为 2，不能按最终数量直接作严格 A/B 比较。
运行开始后发现原 capability note 仍列举原始工具，已改成只暴露菜单入口，
并通过定向测试；该提示清理不在这次启动快照内，未再跑一次全量本地实验。

没有修改全局默认行为：`researcher.harness.guided_reading` 默认 false，本实验显式 true。
当前受控模式覆盖数据库检索与 Reader，不与 evidence checkpoint 同时启用。
完整范围、索引问题和文献磁盘分类见 [data_flow.md](../../data_flow.md)。

## 验证产物

- [analysis.json](analysis.json)：完整运行统计和失败摘要。
- [analyze_run.py](analyze_run.py)：由本地已落盘运行记录重新生成统计，无网络调用。
- [builder-replay.json](builder-replay.json)：真实混合草稿拆分后的 Cards、Evidence 与 Validator 结果。
- [offline-tests.log](offline-tests.log)：1303 通过、4 失败、6 排除；失败均为此前已确认基线问题。
  三个依赖已删除备份，一个断言旧的 ArxivWebSearchTool 属性。
- 提示清理后重新执行 `test_research_reading_policy.py`、`test_researcher_prompt_policy.py`、
  `test_task_researcher_workflow.py`、`test_tool_call_harness.py`，全部通过。
