# Renderer 真实材料验收与运行归档

本目录仅保留在 `experiment/renderer-acceptance-20261019` 分支。实现、自动化测试、精简成果摘要和更新后的待办清单合入 main，交付说明见 [Renderer 交付与验收摘要](../../Renderer_交付与验收摘要.md)。

## 可直接查看的编译报告

以下目录由已校验材料包逐字节展开，报告及其相对附件链接可直接浏览。

| 报告 | 历史材料来源 | 查新点 | 有效证据卡 | 入口 |
| --- | --- | ---: | ---: | --- |
| A | 10 月 16 日 Validator Gate A 完整运行 | 2 | 0 | [编译报告](reports/A/MF2033k6lC/report/acceptance.md) |
| B | 10 月 16 日受控阅读完整运行 | 4 | 0 | [编译报告](reports/B/MF2033k6lC/report/acceptance.md) |
| C | 10 月 18 日 Coordinator 补检完整运行 | 3 | 1 | [编译报告](reports/C/MF2033k6lC/report/acceptance.md) |
| D | 9 月 28 日 arXiv 完整运行 | 4 | 3 | [编译报告](reports/D/MF2033k6lC/report/acceptance.md) |

四份完整材料均通过编译和重复编译检查，A/B 混合材料被版本门控拒绝。材料和结论保持原样，最终查新点评审均为证据不足。历史索引只在核对来源后的测试副本中补建，不能视为历史写入时已有新门控。

## 证据与复现

- [首轮 A/B/mixed 验收](existing-materials/README.md)、[机器可读结果](existing-materials/result.json)：包括混合材料包和原始文件摘要。
- [含证据卡的追加验收](additional-completed/README.md)、[机器可读结果](additional-completed/result.json)：包括原始证据、局部评审与来源文件核验。
- [三组编译检查脚本](validate_materials.py)、[历史来源核验与导入脚本](replay_existing.py)、[独立历史运行编译脚本](replay_completed.py)。来源审计的完整复现需要本机原始运行目录；归档材料包含独立编译所需的登记输入，未包含所有历史 Runtime 和参考文献正文。
- [实验元信息](experiment.json)保留分支基线、运行状态、参数与各轮验收范围。

解压材料包后，可在本分支用 `validate_materials.py --compile <解压后的 paper 工作目录>` 独立编译。编译和历史材料验收脚本禁止 socket 网络操作，无需模型调用。

## 新流程失败与中断记录

- 原本地 Qwen 完整流程失败于 Coordinator 合成，首次输出和一次纠错均遗漏 NP-3、NP-4；Research 中另有 Reader 预算耗尽、未登记 artifact_id。见[失败摘要](failed-local-summary.json)、[运行清单](failed-local-run.json)和 [原始日志](live-a.log)。对应待办已回写 main。
- 随后曾在实验副本中将 Coordinator 配置改为 DeepSeek，执行了一次成功探测；用户叫停后中断重跑，之后禁止更换或调用模型。该次重跑记录仅有本地 Qwen 的 24 次成功调用和 1 次取消，尚未执行到 DeepSeek 汇总，排队的第二次运行没有启动。见[探测记录](coordinator-probe.json)、[当时的实验配置](local-with-official-coordinator.json)、[中断日志](live-a-retry.log)。该配置仅作历史证据，不是当前推荐配置或复现上述离线验收的入口。
- [失败与中断的 Runtime 归档](../runtime/MF2033k6lC_2026-10-18)保留当时的事件、调用、配置和工作区快照。后续四份材料验收不改写这两次新运行的失败／中断状态。

所有时间记录带原始时区；本轮日期按 Asia/Shanghai 为 2026-10-19。这里的成功是历史材料编译及核验成功，不代表当前 HEAD 的新完整 Agent 流程已经跑通。
