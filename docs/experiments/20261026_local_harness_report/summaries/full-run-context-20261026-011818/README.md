# MF2033k6lC 完整运行与上下文溢出审计

执行时间：2026-10-26 01:19:36 至 2026-10-26 01:22:28（Asia/Shanghai），耗时 171.124 秒。

完整工作流运行至报告生成，进程退出码 0，运行状态 SUCCESS。该状态表示流程正常返回，**不代表证据充分或查新结论成立**。本次报告保留 insufficient_evidence / material_unavailable。

## 输入与执行条件

- 论文：《面向大规模动态图的图神经网络优化机制研究》（MF2033k6lC），90 页，原始 PaperInput 正文 81,079 字符、91 条参考文献。
- 使用既有完整解析文本重新执行提取、规划、检索、核验与报告生成；复用正常入口的参考文献缓存，未复用旧查新点、检索计划或报告。本次未重跑 PDF 解析。
- 输入 SHA-256：89fcc578efcc840daa9c4e0698943314f794e096e176c067dbbd6e603d42ea66。
- 仅调用本地 qwen2.5-7b-instruct；云端模型调用 0 次。用户授权 arXiv 与 Springer 文献检索，本次实际检索执行使用 arXiv。
- 保留默认最多 2 轮、并发 4、80 次模型调用、48 次物理文献来源请求的限制。
- 启用 context_admission=observe，使用 vLLM 0.8.5.post1 精确计数，不截断输入、不自动缩减输出预算、不因超限自动切换模型。

## 上下文相关三项记录

| 项目 | 本次结果 |
| --- | --- |
| 上下文超限导致的失败调用 | 0 |
| 上下文超限导致的流程中断 | 0 |
| 上下文超限导致的输出缺失 | 未观察到；查新点、result.json 和报告均已落盘 |
| 精确计数覆盖 | 16 / 16 次；全部与服务端 prompt_tokens 匹配 |
| 测量超过窗口的调用 | 0 |
| 服务端上下文错误 | 0 |
| 输出达到生成上限（finish_reason=length） | 0；与输入上下文超限单独统计 |
| 峰值单次输入 | 9,126 tokens |
| 峰值调用预留输出 | 4,096 tokens |
| 峰值输入＋预留输出 | **13,222 / 32,768 tokens（40.4%）** |
| 峰值剩余空间 | 19,546 tokens |

16 次模型请求均成功返回，不等于其业务输出全部合法。累计用量为输入 69,000、输出 1,868、总计 70,868 tokens；累计用量不是单次上下文长度。

## 与上下文无关的失败和结果缺口

1. 第一轮 10 条 arXiv 检索执行均正常返回但零命中。任务标记英文检索，实际检索式含中文，例如 `ti:"GSAERU模型" AND abs:"图摘要技术"`。零命中不能归为上下文超限。
2. evidence.missing 补检分支出现 coordinator.task.type_invalid；另一 coverage.no_match 分支仍生成了重规划任务。
3. 第二轮任务 T-R2-QUERY-1 为 partial，直接错误是 `native tool harness failed: ValueError: action policy returned an invalid budget scope`，归档代码为 execution.unknown。进一步核对原始 retrieval_events 和第 15 次模型请求可见，该任务实际已执行 6 条查询，均零命中；随后预算范围异常被通用异常分支捕获并返回空 trace，导致最终任务结果的 search_executions 为空。原始轨迹保留了这 6 次执行，报告统计却未包含它们。此前“未产生新的检索执行”的表述仅依据最终任务结果，现据原始轨迹更正。
4. 达到 2 轮配置上限后保留缺口，最终有效证据卡为 0，NP-1 的 Reviewer 状态为 insufficient_evidence / material_unavailable，没有可成立的新颖性裁决。
5. 提取器输出 1 个审核通过点；其实际补读正文 4000 字符，仍有 20 个未读分段，coverage_complete=false。输入是完整论文，不意味着模型完整阅读了全部正文。
6. 生成报告的自由文本摘要出现“已有文献未涉及此技术细节”，但本次没有检索证据支撑这句话；应以结构化的“证据不足、未作裁决”为准。

本次未形成长篇候选全文的累计阅读上下文，因而**不能用本次零溢出证明长材料场景不会溢出**。保留原始输出，不在审计中改写或修复实验结果。

## 每次模型调用

| 调用 | 阶段 | 输入 tokens | 预留输出 tokens | 合计 | 状态 |
| --- | --- | ---: | ---: | ---: | --- |
| [llm_0001](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0001_local-qwen2.5-7b.json) | extract_points | 4916 | 4096 | 9012 | SUCCESS |
| [llm_0002](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0002_local-qwen2.5-7b.json) | extract_points | 5771 | 4096 | 9867 | SUCCESS |
| [llm_0003](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0003_local-qwen2.5-7b.json) | extract_points | 9126 | 4096 | 13222 | SUCCESS |
| [llm_0004](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0004_local-qwen2.5-7b.json) | plan_research_task | 1412 | 2048 | 3460 | SUCCESS |
| [llm_0005](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0005_local-qwen2.5-7b.json) | run_research_task | 2055 | 4096 | 6151 | SUCCESS |
| [llm_0006](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0006_local-qwen2.5-7b.json) | run_research_task | 2215 | 4096 | 6311 | SUCCESS |
| [llm_0007](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0007_local-qwen2.5-7b.json) | run_research_task | 2377 | 4096 | 6473 | SUCCESS |
| [llm_0008](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0008_local-qwen2.5-7b.json) | run_research_task | 2363 | 4096 | 6459 | SUCCESS |
| [llm_0009](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0009_local-qwen2.5-7b.json) | plan_supplement | 6156 | 4096 | 10252 | SUCCESS |
| [llm_0010](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0010_local-qwen2.5-7b.json) | plan_supplement | 6059 | 4096 | 10155 | SUCCESS |
| [llm_0011](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0011_local-qwen2.5-7b.json) | plan_supplement | 6160 | 4096 | 10256 | SUCCESS |
| [llm_0012](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0012_local-qwen2.5-7b.json) | plan_supplement | 6060 | 4096 | 10156 | SUCCESS |
| [llm_0013](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0013_local-qwen2.5-7b.json) | plan_research_task | 2543 | 2048 | 4591 | SUCCESS |
| [llm_0014](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0014_local-qwen2.5-7b.json) | run_research_task | 3125 | 4096 | 7221 | SUCCESS |
| [llm_0015](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0015_local-qwen2.5-7b.json) | run_research_task | 3285 | 4096 | 7381 | SUCCESS |
| [llm_0016](runs/0001/MF2033k6lC/runtime/run-7660fcdb38c448b4bff501cb4878f25b/llm_calls/0016_local-qwen2.5-7b.json) | synthesize_report | 5377 | 4096 | 9473 | SUCCESS |

## 产物

- [查新报告（证据不足）](runs/0001/MF2033k6lC/report/MF2033k6lC-report.md)
- [完整结构化结果](runs/0001/result.json)
- [上下文审计及失败、任务状态明细](context-audit.json)
- [运行状态和耗时](runs/0001/run.json)
- [有效配置](runs/0001/effective-config.json)
- [启动源码与输入快照](runs/0001/startup/manifest.json)
- [提取器阅读及审核轨迹](runs/0001/MF2033k6lC/point_extraction_trace.json)
- [控制台日志](console.log)
- [代码层根因定位与离线复现](root-causes.md)

