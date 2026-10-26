# Issue #20 Live 测试记录

测试时间：2026-10-19（Asia/Shanghai；runtime 目录按 UTC 生成，因而日期为 2026-10-18）

## 结论

真实 `qwen2.5-7b-instruct` 已通过持久化全文读取核心验收。测试论文共 8330 字符，
关键贡献 `HELIOS-LATE` 位于 `[8000:8330]`，不在 PaperDigest 的摘要摘录中。模型通过
版本化索引依次读取三个正文段，最终生成 3 条带 `[8000:8330]` 来源的候选，证明后段
正文实际进入模型上下文且来源句柄可用。

测试同时发现一个明确的质量边界：3 条候选在语义上都是 `HELIOS-LATE` 的改写。
去重模型建议删除第 2、3 条，但保守守卫因 claim 文本并非字面相等而拒绝删除，留下
`pending_indices=[2,3]`。这是预期的“不在无程序证明时擅自删除”行为，但也意味着一次
live 通过不能解释为最终查新点质量或语义去重已经合格。

## 环境和命令

- 模型端点：通过用户建立的 SSH tunnel 访问 `http://127.0.0.1:8000/v1`。
- `/v1/models`：`qwen2.5-7b-instruct`，vLLM，`max_model_len=32768`。
- Python：`/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/python`。
- live 用例：`Novelty-Multi-Agent-Framework/tests/test_live_point_paper_reading.py`。

执行命令：

```sh
set -a
source backend/.env
set +a
ISSUE20_LIVE_ARTIFACT_DIR=docs/experiments/20261019_issue20_paper_reading/live \
/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/python \
  -m pytest -m live tests/test_live_point_paper_reading.py -s -vv -rs
```

结果：`1 passed in 24.69s`。测试代码记录的业务执行时间为 23.046 秒，runtime manifest
记录 23.207 秒。首次未显式加载 `.env` 的运行被跳过，没有发出模型请求；修正环境加载
后只执行一次真实验收。

本阶段所有真实模型调用均通过用户建立的 SSH tunnel 访问当前 vLLM 服务；没有执行
其他模型供应商的测试。仓库既有摘要兼容入口不作为本次持久化全文验收依据。

## 模型调用和用量

| 指标 | 实测 |
| --- | ---: |
| 总模型调用 | 7 |
| 阅读选择调用 | 3 |
| 生成/去重调用 | 4 |
| 输入 token | 18,194 |
| 输出 token | 874 |
| 总 token | 19,068 |
| 失败调用 | 0 |
| 模型调用累计耗时 | 22,986 ms |

本地模型没有价格配置，7 次调用均为 unpriced，runtime 的成本完整性为 `PARTIAL`。
这表示人民币成本未知，不表示计算资源免费。

## 阅读和定位证据

| 分段 | 字符范围 | 结果 |
| --- | --- | --- |
| body-1 | `[0:4000]` | 已读，背景内容 |
| body-2 | `[4000:8000]` | 已读，相关工作 |
| body-3 | `[8000:8330]` | 已读，包含 `HELIOS-LATE` |

- `digest_contains_late_marker=false`，排除仅从原摘要摘录得到贡献的可能。
- `unread_segments=[]`，本样本正文分段全部读取。
- 3 条候选均包含 `HELIOS-LATE`，并引用同一版本化 body-3 位置。
- 索引引用写入 runtime manifest，包含 run ID、相对路径和 SHA-256。
- `coverage_complete=false`、`semantic_completeness_verified=false`。全文已读只证明读取范围，
  不自动证明贡献抽取完整或候选相互独立。

## 去重发现

模型返回两个删除建议：2→1、3→1，理由为技术目标、机制和范围等价。程序保守守卫发现：

- technical_features 和 technical_features_en 均完整覆盖；
- 中文及英文 claim 不是字面相等；
- 现有程序没有可靠的语义等价证明。

因此 `accepted_deletions=[]`、`pending_indices=[2,3]`，最终保留 3 条候选。trace 最终
`scope_status=pending_coverage`，没有把 dedup pending 单独提升为最终 scope status；详细
去重待定事实仍在 `deduplication.coverage_issues` 中。若产品要求用户直接消费最终候选，
需要后续决定是否增加可审计语义等价验证或并列暴露多个 pending 状态。

## 产物

- `live/live-result.json`：精简验收结果与最终候选。
- `live/point-extraction-trace.json`：读取、生成、去重和覆盖完整轨迹。
- `live/outputs/.../runtime/<run-id>/llm_calls/`：7 次原始模型调用记录与 provider usage。
- `live/outputs/.../runtime/<run-id>/manifest.json`：运行身份和输入索引引用。
- `live/archive/.../<run-id>/`：完成时归档副本。

归档 JSON 内的 `workspace` 字段保留测试执行时的原始绝对路径，用于审计当时的运行身份；
文件在仓库中的归档位置以本目录为准，不改写历史运行产物中的哈希输入。

本次只验证合成长文上的真实模型行为，没有执行 PDF/OCR、检索、Reviewer 或完整工作流；
真实 PDF 的持久化与跨进程恢复由离线测试覆盖，实际论文上的提取质量仍需 P0-08-E 样本对照。
