# Reviewer 受控阅读补测

日期：2026-11-09（Asia/Shanghai）。基线 main：`7ff63dd0`。
用户明确授权沙箱外执行、本地模型验证、提交并推送当前分支；未调用付费模型。

## 环境对照与离线回归

沙箱内 `127.0.0.1:8000/v1/models` 连接失败，四个异步测试停在 selector 等待；
同一模型地址与同一组测试在沙箱外正常工作，四项全部通过。
随后全量非 live 回归最终为 **2045 passed / 3 skipped / 8 deselected，74.30 秒**。
三项跳过需要仓库未分发的旧原文 backups；未伪造或替换原文。
八项其他 live 测试涉及外部模型/检索/OCR/Extractor，未纳入本阶段。

## 实测内容与边界

模型：`qwen2.5-7b-instruct`，endpoint 固定为本机 `http://127.0.0.1:8000/v1`，无云端 fallback。
temperature=0，输出上限 2048 token，单卡最多 10 次模型请求（预算用例为 2 次），
Reader 最多 2 次、请求字符最多 8000，总调用上限 64 次/运行。

夹具由脚本生成合成 Work、SourceRecord、Artifact、Evidence、Card，通过实际 ReferenceStore 和 Reader。
最终夹具有 Card.sources 定位和实际原始读取回执，通过生产来源闭包校验。
特征为中位数聚合、训练所得阈值、差分隐私；正文明确使用中位数、阈值固定且不学习、未提及差分隐私。
标签是本次合成案例的检查预期，不是人类标注的论文语义金标准。

最终 run-7 的九个场景：已有引文充分、回读正文、三个特征未知、正文文件缺失、两次调用预算耗尽，
以及同一 Reviewer 实例同时处理四张卡。四个并发请求复用 point/task ID、分别具有 paper/Card/Work 身份。
验证最终结果没有点级 verdict/confidence、模型请求不越预算、增量证据归属正确、原文/哈希/范围与磁盘一致。
无 Reader 的场景没有虚构读取；正文用例确实读取已授权正文。

run-6 另验证实际模型生成的单卡结果进入 checkpoint：第一次汇总在发送前注入 TimeoutError，
记录持久化；恢复只调用一次真实汇总模型，不调用工具、不重读材料，冻结记录字节保持不变。
该次汇总输出 insufficient_evidence，无点级 verdict；不把此个案扩展成新的 unknown 裁定政策。

## 全部轮次，保留失败

| 轮次 | 成功本地请求 | 主要结果 |
| --- | ---: | --- |
| run-1 | 0 | 客户端要求 API key；为无鉴权本地服务补上占位 key。9 次尝试在发送前失败 |
| run-2 | 63 | 最终阶段仍返回 option_id，缺失材料用例耗尽预算；语义匹配 2/3 |
| run-3 | 34 | 使用最终说明 Schema 后选项混用消失；非 unknown 无引用被拒绝，另有模型选择不读正文；语义匹配 0/3 |
| run-4 | 38 | Schema 导出既有非 unknown 引用要求、菜单区分摘要/正文；九场景运行契约通过，语义匹配 2/3 |
| run-5-checkpoint | 3 | 合成夹具漏 Card.sources 定位，生产 Gate 拒绝创建 checkpoint；没有绕过校验 |
| run-6-checkpoint | 4 | 补齐夹具并先通过离线来源闭包测试；单卡及汇总恢复通过，语义匹配 1/1 |
| run-7 | 38 | 最终生产闭包夹具的九场景运行契约通过；语义匹配 2/3 |

run-4 和 run-7 的运行契约通过**不表示语义正确**。run-7 正文明确写着中位数聚合，
模型已经读到并引用该段，却把 F1 判为 contradicted。程序能确认引用及归属真实，不能确认模型的关系判断正确。
这个错误保留为模型判断风险，没有自动改成 supported，也没有为点级 unknown 制定规则。

## 成本

最终 run-7：38 次请求，输入 105,152 / 输出 4,338 token，66.092 秒。
run-6：4 次请求，输入 10,165 / 输出 781 token，18.498 秒。
两组最终验收合计 42 次请求、120,436 token。

包含全部探索及失败夹具：**180 次成功本地请求，输入 471,754 / 输出 21,157 token，总计 492,911 token**。
付费模型调用为 0；未计量本地硬件与电力成本。各轮输入和菜单不同，不作为严格质量/性能对照实验。

## 复现与记录

```bash
PYTHONPATH=backend/src:. python scripts/run_reviewer_controlled_local.py \
  --live --output outputs/reviewer-local-new-run
PYTHONPATH=backend/src:. python scripts/run_reviewer_controlled_local.py \
  --live --checkpoint-only --output outputs/reviewer-local-new-checkpoint
```

输出目录必须不存在。模型调用、原始输入/输出、usage、夹具和逐卡结果保存到该目录。
本次原始记录在本地 `outputs/reviewer-controlled-local-20261109/`；可提交的压缩结果见
[results.json](results.json)，包含所有轮次、检查结果、实际关系、阅读事实及最终源码摘要。
全量离线日志在本地 `/tmp/reviewer-final-verified.log`。

未覆盖真实论文集语义质量、其他模型、在线全文获取、跨进程单卡续读和大规模并发压力。
“汇总丢文献编号”与“全 unknown 却出现 partially_novel/0.8”仍是两个独立后续验收问题，
不能通过补编号相互替代，也不宣称本次已解决这两个最终报告问题。
