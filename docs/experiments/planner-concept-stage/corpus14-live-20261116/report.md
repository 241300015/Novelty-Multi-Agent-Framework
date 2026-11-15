# 14 篇论文完整 Planner 真实测试

日期：2026-11-16。分支 `1st-stage`，代码 HEAD `52ebd3b6`。本批次运行期间没有修改 Planner、Prompt 或模型配置。新增批量驱动为 `scripts/run_planner_corpus.py`，其 SHA-256 已记录在 [batch.json](batch.json)。输入为 [corpus-14](../corpus-14/README.md)，14 篇论文、16 个查新点。

## 结论

全部 16 点均已运行结束；5 点生成完整 SearchPlan，11 点失败。工程完成率 **31.25%（5/16）**；14 篇论文中 5 篇至少有一点完成。5 个成功结果共包含 30 个概念、15 条策略，均包含 strict、medium、broad 三档。

累计 **77 次实际模型调用**：概念阶段 72 次，策略生成 5 次。全部使用 `qwen2.5-7b-instruct`，temperature=0.2、max_tokens=2048、timeout=180 秒。每点上限 24 次，最多 4 点并行。没有案例耗尽运行级 24 次预算；失败均在概念阶段的输出契约恢复预算耗尽后停止。

这不是语义验收通过率：未执行用户人工签收、实际检索或查新结论判断。本轮也没有测试尚未实现的 V2 证据 ID 方案。

## 逐篇结果

下表调用数包含概念与策略阶段。每个案例目录都保存 result、概念审计、运行级模型请求响应；成功案例另有 plan.json。

| 论文 | 查新点 | 调用数 | 结果 | 终止原因/产物 |
| --- | --- | ---: | --- | --- |
| Land Use | NP-1 | 5 | 失败 | terms 复核未精确覆盖候选 |
| Activity Recognition | NP-1 | 4 | 失败 | 反复生成非法 role=`operation` |
| Intrusion Detection | NP-1 | 3 | 完成 | [6 概念、3 策略](03-Intrusion-Detection/NP-1/plan.json) |
| LightGCN | NP-1 | 3 | 完成 | [6 概念、3 策略](04-LightGCN/NP-1/plan.json) |
| N-BEATS | NP-1 | 3 | 完成 | [6 概念、3 策略](05-N-BEATS/NP-1/plan.json) |
| MobileNetV2 | NP-1 | 17 | 失败 | 局部纠错后仍发生 terms 覆盖错误；保留 4 个已接受候选和 2 个未解决问题 |
| Attention UNet | NP-1 | 4 | 失败 | 反复生成非法 role=`operation` |
| Fake News Detection | NP-1 | 3 | 完成 | [6 概念、3 策略](08-Fake-News-Detection/NP-1/plan.json) |
| DDP-GCN | NP-1 | 4 | 失败 | 反复生成非法 role |
| Keyword Spotting | NP-1 | 5 | 失败 | terms 复核未精确覆盖候选 |
| Keyword Spotting | NP-2 | 3 | 完成 | [6 概念、3 策略](13-Keyword-Spotting/NP-2/plan.json) |
| TabNet | NP-1 | 5 | 失败 | terms 复核未精确覆盖候选 |
| TabNet | NP-2 | 4 | 失败 | 反复生成非法 role=`mechanism` |
| Music Transformer | NP-1 | 4 | 失败 | 反复生成非法 role=`application` |
| jsjyyyj.2022.02.0052.v2 | NP-1 | 5 | 失败 | terms 复核未精确覆盖候选 |
| 四川盆地丘陵区耕地遥感识别 | NP-1 | 5 | 失败 | terms 复核未精确覆盖候选 |

失败分类：非法分类 5/16（31.25%），词项覆盖错误 6/16（37.5%）。这些分类是最终终止原因，不表示过程中没有其他错误。

## 纠错实际效果

5 个完成点的概念阶段均为一次生成、一次初审通过，再调用一次策略生成。没有完成点依赖语义纠错成功，因此不能据此声称纠错恢复有效。

MobileNetV2 是本批次进入局部纠错的案例。初审接受 4 个候选，留下“逐通道卷积”“非线性激活”相关问题。5 次完成并被记录的局部纠错复核中，3 次候选内容未变、2 次改变，全部仍未解决；第 6 次纠错后的局部复核因 terms 错配耗尽契约预算而终止。没有记录为成功修复的内容。

程序正确拦截一次“候选未变化却声称 fixed”的结果，并将其保持为 unresolved。失败终态保留了通过集和开放问题，但没有输出正式计划。语义轮次尚有/已有通过项，并不允许绕过契约错误或继续无限调用。

语义审核也存在可直接对照输入确认的误判：MobileNetV2 输入技术特征明确包含“中间扩展层使用轻量级逐通道卷积处理特征”和“移除窄层中的非线性激活以保持表达能力”，审核理由却称原文没有提到逐通道卷积、非线性激活的具体应用或作用。这说明问题不只在 JSON 和字段协议；模型还会否认实际提供的依据。

## 完成结果的质量观察

现有策略编译器产生了以下 broad 策略：

| 案例 | broad 实际展开 |
| --- | --- |
| Intrusion Detection | SVM-SMOTE过采样 |
| LightGCN | LightGCN 算法 |
| N-BEATS | N-BEATS模型 |
| Fake News Detection | 新闻摘要 |
| Keyword Spotting NP-2 | 微调模型 |

其中“新闻摘要”和“微调模型”已丢失原任务中的虚假新闻/事实核验、开放词汇关键词检测等限定；仅靠这些词不能保证检索保留原查新机制。模型名查询也不能替代对相关技术机制的全面检索。这里是对输入与输出的审阅发现，未通过真实检索测量召回或精确率。

当前实现尚缺原方案要求的逐档独立语义复核，因此“生成三个档位”只能计作工程完成。下一轮应同时关注输出协议和策略语义，不能只通过提高重试次数处理问题。

## 可复现性与审计

运行命令：

```bash
PYTHONPATH=.:backend/src python scripts/run_planner_corpus.py \
  --output outputs/planner-corpus14-20261116 --workers 4
```

复跑时应使用新的 output 目录，驱动拒绝覆盖既有批次。场景 A 对每点清空英文 claim/technical_features，再使用中文任务；未把 evidence.json、PDF 页文本或人工答案传入模型。此次只测 A 场景一次，其他语言、重复性和受控注入不在统计中。

批量清单包含 commit、分支、驱动摘要、各点输入摘要、配置、耗时和错误。原运行目录为 `outputs/planner-corpus14-20261116/`，本报告同级目录保存副本：16 份结果、16 套概念审计、77 份运行级模型调用记录、5 份正式计划。概念审计索引共 125 个文件摘要全部一致。

复现以 batch.json 的代码版本及输入摘要为准。

## 后续优先事项

1. 对非法 role 的确定错误提供字段级合法选项，验证能否有效恢复；不要让整批响应反复生成同一非法值。
2. 按 V2 计划引入词项/别名 ID、证据 ID 和局部契约修复，使用本批次失败记录做固定输入对照。
3. 单独检验“输入已有机制却被判缺少支撑”的语义误判；证据 ID 合法不能自动解决此问题。
4. 按原方案补齐逐档策略语义复核，检查 broad 放宽后是否仍保留核心机制。
