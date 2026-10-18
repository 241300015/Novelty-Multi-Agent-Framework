# 其他已完成工作流的完整材料编译

2026-10-19（北京时间），两份额外历史完整工作流的离线编译验收 **PASS**。本轮没有调用模型或网络，没有修改模型配置或产品代码。

## 已生成报告

| 来源 | 查新点 | accepted 证据卡 | 局部评审记录 | 编译结果 |
| --- | ---: | ---: | ---: | --- |
| 2026-10-18 Coordinator 补检流程 | 3 | 1 | 1 | [报告 01](../reports/C/MF2033k6lC/report/acceptance.md) |
| 2026-09-28 arXiv 完整流程 | 4 | 3 | 1 | [报告 02](../reports/D/MF2033k6lC/report/acceptance.md) |

证据卡正文、来源、引用、有效证据数量和局部评审记录均进入 default 报告。两份报告的查新点最终状态仍全部为 `insufficient_evidence`；局部记录的 `verdict`、`verdict_reason` 为 null，正确显示为 `—`。卡片通过 Validator 和工作流运行完成均不等于新颖性裁定已完成。

## 材料来源和兼容性

- 报告 01：`outputs/coordinator-choice-rollout-20261018/runs/0001`，历史提交 `aae5cc542eb55bd2f581eb502b9e8e1924a75518`，运行 `run-0de03606da4a4c049897a0a8b11af4c8`。材料所属时间为北京时间 10 月 18 日 21:24:52–21:31:15。
- 报告 02：`outputs/arxiv-web-full-workflow/0001`，历史提交 `7e71dc96b24f87e8ea8fed2fa5b761dfaef53a83`，运行 `run-09834db78b2742109def38fd991ff11a`。材料所属时间为北京时间 9 月 28 日 15:03:08–15:09:01。

两次原运行均为 `SUCCESS`，均使用本地 `qwen2.5-7b-instruct`。两份来自不同的历史提交，本轮分别核验并编译，不将它们描述为同一代码版本。论文、查新点、任务、检索规划、文献目录、证据和报告均通过当前数据契约；材料与各自最终 `persist_report` 运行快照相符。

本轮针对非空证据补齐了验收脚本的核验参数：向当前 `validate_report_integrity` 传入原始 Evidence 和只读使用的 ReferenceStore。这样可以核对局部评审的来源文件、SHA-256、读取范围、读取引用及原文，而非仅验证卡片 ID 和最终结论字段。两份材料的完整性检查均通过。

历史材料没有新门控的原生版本索引；与上一轮相同，仅在测试副本中为已核验的完整材料补建索引，保留历史运行时间和导入来源标记。全部业务文件逐字节保留，原始目录未修改。补建索引不是历史写入时已有版本保护的证明。

## 检查与限制

- 两份材料各在新进程中编译两次，业务内容、材料所属时间和索引不变，报告生成时间更新。
- 每份报告 13 个本地附件链接全部有效，没有遗留模板占位符。
- 报告 01 的 1 张证据卡和报告 02 的 3 张证据卡均已检查出现在正文中，各自 1 条局部评审记录也已检查。
- 原始材料和测试副本的所有登记输入 SHA-256 保持一致。
- 本轮完成的是已有完整材料的来源核验、版本门控和 default 编译，没有重新执行 Agent 全流程。

结构化结果及材料文件哈希见 [result.json](result.json)。可下载包含全部 Renderer 登记输入、索引及报告的 [报告 01 材料包](completed-01.tar.gz)、[报告 02 材料包](completed-02.tar.gz)。参考文献正文和完整 Runtime 仍位于原始运行目录，材料包不包含它们。

## 复现

在仓库根目录执行，工作目录必须不存在：

```bash
/home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  docs/experiments/20261019_renderer_versions/replay_completed.py \
  --run outputs/coordinator-choice-rollout-20261018/runs/0001 \
  --run outputs/arxiv-web-full-workflow/0001 \
  --work-root outputs/renderer-versions-20261019/additional-completed-replay \
  --result outputs/renderer-versions-20261019/additional-completed-replay/result.json
```

脚本和编译子进程均通过 audit hook 禁止 socket 网络操作。
