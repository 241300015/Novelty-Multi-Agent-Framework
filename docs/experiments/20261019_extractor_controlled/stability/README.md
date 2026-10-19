# 同生产配置的五次独立进程提取

目的：检查新 Extractor 在真实 PaperInput 上是否容易中断，以及有界纠错能否消化自然出现的错误。
这不是语义完整性认证，也不把合成故障注入混入自然错误率。

使用全流程实验的同一 profile、同一 81079 字符 PaperInput、生产工厂及
`NoveltyWorkflow._extract_points` 节点。各次均由不同子进程仅凭父进程准备的持久化目录恢复输入。
没有固定阅读段或修改模型响应。五次顺序执行，全部调用本地 Qwen，未运行检索。
配置为 temperature=0、max_tokens=4096；新运行身份会体现在选项 ID 和引用中，
因此不是五次字节完全相同的请求。源码／配置／输入在 `startup/` 冻结。

| 次数 | 进程 PID | 状态 | 模型调用 | 纠错 | 时间（秒） |
| --- | ---: | --- | ---: | --- | ---: |
| [01](01/result.json) | 31640 | 完成 | 4 | 审核一致性错误，纠正成功 | 46.50 |
| [02](02/result.json) | 32175 | 完成 | 3 | 无 | 19.82 |
| [03](03/result.json) | 32448 | 完成 | 3 | 无 | 19.96 |
| [04](04/result.json) | 32738 | 完成 | 3 | 无 | 21.34 |
| [05](05/result.json) | 33005 | 完成 | 3 | 无 | 20.21 |

观察到的流程中断为 **0/5**，自动纠错为 **1/5**，共 16 次本地调用。
01 的审核将 GSAERU、DSGNN 两点都标为通过，但比较关系填成 duplicate，理由又说两者不同。
固定检查拒绝其矛盾输出；纠错调用改为 distinct 后通过，未误删任一点。
这次纠错消耗审核预算，生成预算仍为 1。

局限同样明确：五次都只读 `[0:4000]`，后段自主选取为 0/5；04 又把原文的内存／时间占比
写成降低比例并获审核通过。因此，这组结果只支持“当前单篇样本下未观察到高频流程中断”，
不能据此保证其他论文上的低中断率，也不能宣称语义审核可靠。

[results.json](results.json) 保存原始逐次结果；[audit-summary.json](audit-summary.json) 汇总指标。
各目录的 `trace.json` 及 runtime 调用文件保存自然错误、原始响应、程序反馈和纠正结果。

```sh
LOCAL_VLLM_API_KEY=local PYTHONPATH=backend/src python scripts/run_extractor_stability.py \
  --paper-json tests/fixtures/extractor/real_paper_input.json \
  --profile config/profiles/extractor-harness-full-local.json \
  --output /tmp/new-extractor-stability --repetitions 5
```
