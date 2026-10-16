# NP-1 选项编号错误复现（2026-10-17）

## 结论

编号错误可复现，但不是每次必现。将上一次 NP-1 首轮请求原样提交三次，出现两次“把选项说明填进 option_id”的错误、一次正确编号。随后独立完整重跑 NP-1，首轮与后续选择均正确，没有重现连续 28 次错误的轨迹；最终仍零卡，失败原因变为草稿把目标查新点当成参考文献引文。

本次没有修改 Harness、提示、模型参数或预算，没有筛掉失败样本，也没有将这次较顺利的选择轨迹替换进此前 C 组汇总。

## NP-2 为什么没有正文阅读

上一轮 NP-2 的实际决策是八篇摘要逐篇选择“不获取正文”，摘要处理完后选择停止检索。因此正文队列为空，直接提交摘要证据草稿。没有发生“正文已经获取、却未执行读取”。

当前实现要求读完已选择获取的正文，但允许所有文献都不获取正文，允许只依据摘要生成卡片；没有“至少获取一篇正文才能产卡”的门槛。正常路径下有正文队列则必须处理，外层预算仍可能提前终止。

## 首轮请求原样重放

来源：`outputs/harness-c-rerun-20261017/runs/choice_only/NP-1/MF2033k6lC/runtime/comparison-choice_only-NP-1/llm_calls/0001_local-qwen2.5-7b.json`。

HTTP 请求 JSON 未改动；仍使用本地 `http://127.0.0.1:8000/v1/chat/completions`，`qwen2.5-7b-instruct`、temperature=0.3、max_tokens=4096、tool_choice=none，没有 tools 定义。未指定 seed。

当前菜单：

```json
{
  "v1_0": "执行第 1 轮检索：arxiv（strict, medium, broad）",
  "v1_1": "停止检索，开始阅读已选全文"
}
```

| 重放 | 模型返回的 option_id | 是否符合当前菜单 |
|---|---|---|
| 1 | `v1_0` | 是 |
| 2 | `执行第 1 轮检索：arxiv（strict, medium, broad）` | 否，返回说明而非编号 |
| 3 | `执行第 1 轮检索：arxiv（strict, medium, broad）` | 否，返回说明而非编号 |

这三次只是精确请求的模型响应重放，没有执行工具。错误值按当前菜单成员校验必然被拒绝。2/3 是本次三个样本的观察，不能当成稳定错误率。

输入和原始返回见 [replayed-request.json](replayed-request.json)、[request-replays.json](request-replays.json)。

## 独立完整 NP-1 重跑

继续使用相同冻结数据和同一个 Researcher → Builder → Validator 局部流程。任务哈希、预算、system/user 提示、实际首轮请求 JSON 均与上一轮 NP-1 一致，见 [audit.json](audit.json)。

- 模型调用 20 次，服务调用失败 0 次。
- 18 次选项决策，无无效选项；其中 9 次本地阶段切换，实际工具执行 9 次。
- 实际执行 1 次检索、8 次摘要读取；不获取正文，第一轮结束后停止。
- 没有重复读取、没有预算触顶，也没有触发后两轮策略或正文队列。
- 初稿把查新点中的 GSAERU 声明作为 quote，Builder 判定不在 Reader 返回的参考文献原文中。
- 一次引文修复后，又把查新点中的技术特征作为 quote，仍被拒绝。
- Builder 两次拒绝、产卡 0，Validator 没有输入卡，故通过率为 N/A。
- 运行状态字段为 `completed`，仅表示这次正常走到收尾；不表示产卡成功。

独立三次请求重放加完整流程共 23 次真实模型调用。完整流程记录见 [metrics.json](metrics.json)、[builder-calls.json](builder-calls.json) 和输出目录 `outputs/harness-np1-reproduction-20261017/`。

## 判断边界

同样的首轮输入出现不同选择，说明此次不能把编号错误归因于确定性的错误菜单编号；模型对编号/说明的遵循不稳定。当前 Harness 能拦住非法执行，但通用纠错提示没有保证下一轮改正。

“选择执行正确”与“最终证据引用正确”是两个独立检查点。本次前者通过，后者失败。未运行 Reviewer，未开展新的协议修补。

复现脚本见 [reproduce.py](reproduce.py)。它先重放保存请求三次，再完整运行一次固定 NP-1；默认输出目录必须不存在，以避免覆盖既有记录。
