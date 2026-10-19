# Renderer 交付与验收摘要

2026-10-19。本次交付覆盖 P0-06 的 default 报告编译、阶段产物展示、编译前版本门控和时间区分。当前已测场景未发现 Renderer 功能问题；不将上游纠错、全文索引共用或完整 Agent 流程验收视为已完成。

## 实现

- default 模板展示论文输入／解析状态、查新点、规划、实际检索与候选、材料目录、证据／Validator、单卡及查新点评审、局部成果、补检、结论与限制，并提供原文件附件链接。缺少上游产物时明确标注，原文和日志不全部拼入报告正文。
- `report-inputs.json` 在阶段落盘时登记运行 ID、SHA-256 和依赖版本，报告落盘后封存。Renderer 只消费通过核验的同一批字节；材料混用、文件变更、依赖过期、未封存及缺少版本记录时停止编译。
- 报告生成时间取当前编译时间；材料所属时间取材料登记开始至封存的时间区间，不代表文献发表时间。重复编译保持材料时间及业务内容不变。

对应实现提交：`862639ff`、`35e7a0b5`。操作说明见 [Paper 工作目录](../README.md#paper-工作目录)。

## 验收结果

| 场景 | 结果 |
| --- | --- |
| 四份历史完整材料，分别有 2／4／3／4 个查新点，0／0／1／3 张有效证据卡 | 当前契约及报告完整性校验通过，default 编译通过 |
| 保留 A 的索引，替换 B 的规划、评审与报告 | 版本门控拒绝，未生成报告 |
| 四份材料分别在新进程中重复编译 | 业务内容、材料时间和索引不变，报告生成时间更新 |
| 附件和占位符检查 | 每份报告 13 个本地附件链接有效，无遗留模板占位符 |
| Renderer、版本门控、持久化、完整流程入口的定向回归 | 50 passed；合入前复核通过 |

定向回归命令：`python -m pytest tests/test_renderer.py tests/test_renderer_versions.py tests/test_persistence.py tests/test_run_full_workflow_live.py -o addopts='' -q`。

历史材料原件未修改。它们没有新门控的原生版本索引；测试先核对来源和最终运行状态，再在独立副本中补建索引，混合后不重建索引。因此验收不证明历史写入时已受新门控保护。后续历史材料验收没有模型或网络调用。

## 上游问题及剩余工作

新运行的本地流程停在 Coordinator 报告合成：首次输出和有限纠错均遗漏 NP-3、NP-4，尚未进入 Renderer。此前 Research 阶段还出现 Reader 单工具预算耗尽和未登记 artifact_id 引用。三个真实案例已补入[后端代码审查与待办清单](后端代码审查与待办清单.md)第 7.1、11 节；Reader 停止保护本身不等同于实现缺陷。

四份历史报告的最终查新点评审均为证据不足，保留原始失败、补检及局部成果，不以成功编译替代科学裁定。当前 HEAD 的新完整 Agent 流程仍待上游问题处理后验证。P0-01 的索引共用、完整纠错链／Gate B 持久化、正式路径的完整渲染后自动验收继续按清单跟进。

P0-07 已在 `feat/p0-07-concise-report` 分支实现 `concise` 展示模板，按用户提供的参考报告组织章节，删去技术调试区和重复展示，文献直接列 URL，结论优先使用 Coordinator 综述，无综述时保留逐点比较和裁定兜底；正式展示不列运行技术诊断。中文表达和综述内容先由提示词约束。与 default 共用版本门控并独立保存。308 项定向测试及四份历史材料离线对照通过；详见 [样稿与实验记录](experiments/20261019_p0_07_concise/README.md)。

## 实验资料位置

main 只保留实现、自动化测试、使用说明、本摘要和更新后的清单。本轮原始运行记录、材料包、编译样稿及复现脚本保留在 [`experiment/renderer-acceptance-20261019`](https://github.com/RikoNJU/Novelty-Multi-Agent-Framework/tree/experiment/renderer-acceptance-20261019/docs/experiments/20261019_renderer_versions) 分支：

- [实验入口及运行状态](https://github.com/RikoNJU/Novelty-Multi-Agent-Framework/blob/experiment/renderer-acceptance-20261019/docs/experiments/20261019_renderer_versions/README.md)
- [两份同提交材料和混合材料验收](https://github.com/RikoNJU/Novelty-Multi-Agent-Framework/blob/experiment/renderer-acceptance-20261019/docs/experiments/20261019_renderer_versions/existing-materials/README.md)
- [两份含有效证据卡材料的追加验收](https://github.com/RikoNJU/Novelty-Multi-Agent-Framework/blob/experiment/renderer-acceptance-20261019/docs/experiments/20261019_renderer_versions/additional-completed/README.md)
- [失败运行摘要](https://github.com/RikoNJU/Novelty-Multi-Agent-Framework/blob/experiment/renderer-acceptance-20261019/docs/experiments/20261019_renderer_versions/failed-local-summary.json)
