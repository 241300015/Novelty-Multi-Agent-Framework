# Planner 真实 Qwen 复测（2026-11-16）

## 范围与版本

代码为 `1st-stage` 分支的 `52ebd3b6`，运行期间未修改代码、Prompt 或配置。模型为 `qwen2.5-7b-instruct`，temperature=0.2、max_tokens=2048，每例调用上限 24。使用两篇原有论文的已归档查新点，各运行 NP-1、场景 A（中文输入、中文任务）一次。

本次脚本只运行概念阶段，不运行策略生成和实际检索。新增 14 篇 PDF 本次未进行查新点抽取，未计入测试。V2 证据索引尚未实现，本次是当前旧协议的基线复测。

## 结果

| 案例 | 实际模型调用 | 概念阶段结果 | 失败位置 |
| --- | ---: | --- | --- |
| GSAERU | 5 | 失败 | 候选 4 将 alias“动态图”同时加入 terms，三次契约重试均未纠正 |
| Museformer | 5 | 失败 | 审核引用 `Museformer：结合细和粗粒度注意力的 Transformer 模型`，不是输入 claim/technical_features 的连续原文；三次契约重试未纠正 |

两例合计 10 次实际调用，概念阶段完成 0/2，进入语义纠错 0/2。均在初审契约恢复预算耗尽后停止，并非达到 24 次运行级上限。两例终态审计索引的文件摘要均已核对一致。没有执行人工语义签收。

GSAERU 的 5 次调用包括一次生成、一次初审和三次契约重试。四份审核都把候选 4 的 terms 写成 `[动态时序图, 动态图]`，但原候选 terms 只有 `[动态时序图]`。程序拒绝该响应，因此没有形成通过集，也未进入语义纠错。引文均可逐字匹配，但“循环神经网络”“时间依赖”的引用选择了笼统的结果描述，表明字面匹配仍不能证明语义支撑。

## 产物

原始请求、响应和审计目录：

- GSAERU：`outputs/concept-retest-20261116/gsaeru-live/planner-attempts/e8fa8d91882c470eb0fc5d072c75ae6c/planner/`
- Museformer：`outputs/concept-retest-20261116/museformer-live/planner-attempts/45453ab16ddc44558a08c125d959140b/planner/`

使用的脚本为 `scripts/run_planner_concept_case.py`。输入分别为 `docs/experiments/20260915_224229/run/MF2033k6lC/novelty-points.json` 和 `docs/experiments/20260917_005448/runs/full/0002/MG19333vrw-debug-full-20260907/novelty-points.json`。

## 判断

当前仍可复现“整份审核因单字段错误反复重试”的问题。V2 的证据 ID 可消除原句复制任务，词项/别名 ID 与局部契约修复用于解决字段绑定问题；两者都需要实现后另做固定版本对照。此次没有产生可供人工签收的概念结果，不能计算语义修复率或认定质量提升。
