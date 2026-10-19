# 新 Extractor 的真实全流程衔接实验

使用 `extractor-harness-full-local.json` 和原始 `real_paper_input.json`。
全部推理调用均为本地 Qwen 2.5 7B；arXiv 检索在用户明确授权后执行。
没有指定 Extractor 阅读段、没有替换模型响应、没有跳过生产工作流节点。
`0001` 是离线配置预检；[0002/run.json](0002/run.json) 为实际运行记录。

## 结果

- 流程在 **82.28 秒**结束，14 个执行阶段成功，并生成
  [报告](0002/MF2033k6lC/report/MF2033k6lC-report.md)。
- Extractor 用时 18.29 秒，阅读选择／生成／语义审核各 1 次，无纠错、无中断。
  自主读取持久化正文 `[20000:24000]`，输出 1 个候选，后续规划和任务分发成功。
- 全流程 8 次本地模型调用，共 41356 tokens；没有云端模型调用。
- arXiv 发生 6 次真实物理检索请求，未记录网络错误，但返回 0 条候选；参考文献检索同样为 0。
  最终证据卡为 0，Reviewer 返回 `insufficient_evidence` / `material_unavailable`，`verdict=null`。
- 报告落盘、输入完整性校验和渲染均成功；报告保留“未完成裁定”和阅读覆盖不足的限制。
  单轮上限使补检阶段 `NOT_RUN`，未假称完成补检。

本次证明新 Extractor 在生产配置下能完成并衔接后续流程，且零证据路径能产出诚实报告。
**不证明带证据的 Reader／Reviewer 实质审核路径已跑通，也不证明一次成功足以排除高频断点。**
同配置多次独立进程提取的稳定性结果记录在上级目录。

## 可检查材料

- [汇总](0002/audit-summary.json)：阶段状态、预算、调用与检索数量。
- [Extractor 轨迹](0002/MF2033k6lC/point_extraction_trace.json)：范围决策、原文、候选、审核和版本。
- [结果](0002/result.json)：证据不足的业务终态及报告对象。
- `0002/startup/`：冻结源码、配置和输入；`0002/effective-config.json`：实际配置。
- `0002/MF2033k6lC/runtime/run-e82730d32ad1400a938811c262b38c4b/`：逐次调用、工具、检索请求及阶段记录。

复现命令（新目录会自动递增编号）：

```sh
LOCAL_VLLM_API_KEY=local PYTHONPATH=backend/src python scripts/run_full_workflow_live.py \
  --paper-json tests/fixtures/extractor/real_paper_input.json \
  --runs-root docs/experiments/20261019_extractor_controlled/full-workflow \
  --profile config/profiles/extractor-harness-full-local.json --isolated-config
```

本地端点需要认证时使用实际 `LOCAL_VLLM_API_KEY`，不把凭据写入归档。
