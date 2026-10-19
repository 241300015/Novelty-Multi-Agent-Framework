# 科技查新报告

## 项目信息

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 面向大规模动态图的图神经网络优化机制研究 |
| 英文名称 | — |
| 报告生成时间 | 2026-10-19T07:01:41+00:00 |
| 材料所属时间 | 2026-10-18T13:24:52+00:00 至 2026-10-18T13:31:15+00:00 |
| 查新范围 | 图表示学习、深度学习、图神经网络、分布式技术、graph representation learning、deep learning、graph neural network、distributed |

材料所属时间为本次查新运行开始至报告输入封存的时间区间，不代表文献发表时间。
重新编译会更新报告生成时间，材料所属时间保持不变。

---

## 一、查新目的

由于现实世界中的大多数数据都可以用图表示，因此近年来针对图表示学
习的研究越来越受到关注。通过将高维图数据转化成低维的表征向量，图表示
学习可以有效地对图的信息进行存储并快捷地访问图中实体的关键知识。利用
学习到的图表征数据可以为社交网络行为分析、节点分类、链路预测和聚类等
下游任务提供帮助。随着深度学习的快速发展，研究人员提出了基于图神经网
络的图表示学习方法，其使图中的节点能够同时学习图的结构信息以及邻域节
点的特征信息，从而得到高质量的低维表征。针对图神经网络上的图表示学
习，本文主要关注两个问题：大规模时序图场景下的图表示学习方法和分布式
场景下的大规模图的图神经网络优化训练方法。
真实世界中的图数据规模往往十分庞大且处于动态变化的状态，而现有的
图神经网络要么无法处理动态时序图上的图表征学习，要么面对大规模图数据
集时训练效率低下。为了解决大规模动态图上的图表征学习问题，本文提出了
一种基于图摘要的大规模时序图表示学习方法——GSAERU 模型。GSAERU 模
型由“图表征学习模块”和“时序学习模块”两部分构成。首先将动态时序图
建模为各时刻上原图快照的序列，接着在单时间步上将图快照输入图表征学习
模块。该模块通过图摘要技术对原图进行压缩，并将压缩后的新图输入图自编
码器，利用和原图的重构误差训练原图节点的表征。在获得单时间步上的图表
征后将其输入时序学习模块，并通过一个循环神经网络学习原图在时间维度上
的特征信息与依赖关系，从而得到大规模时序图的高质量表征。基于在四个真
实世界的大规模图数据集上的大量实验表明，GSAERU 模型能够高效生成时序
图的表征。与传统的图神经网络算法相比，其平均内存消耗和平均训练时长只
有其9.86% 与13.27%，且与性能最好的算法在大部分指标下的差距都不超过
3.5%。

ii
使用分布式技术加速神经网络训练也是如今热门的研究课题，因为在处理
大规模数据时，单个设备（如CPU，GPU）有限的内存和计算资源往往会成为
训练瓶颈。而分布式技术可以提供更多的计算资源来提高训练效率。然而由于
图数据的不规则性，传统分布式机器学习方法中的子任务划分和模型训练方法
难以直接应用于图神经网络。针对大规模图流数据场景下的分布式图神经网络
训练优化问题，本文提出了基于图摘要的分布式图神经网络优化训练算法——
DSGNN 模型。DSGNN 模型采用“领导——工作者”工作模式，首先由领导节
点利用一个基于边分割的图流划分算法对原图进行划分并将划分子图分配给各
个工作节点。工作节点随后执行一个基于图摘要技术的小批量训练并将训练结
果通过一个注意力层返回给领导节点，最后由领导节点汇总结果并计算梯度，
并根据梯度同步更新所有计算节点上的模型。基于四个真实世界的大规模图数
据集上的大量实验，结果表明DSGNN 模型可以大大加速GNN 模型的训练，
并且在链路预测任务上的F1 分数显著高于其它分布式图神经网络模型。

---

## 二、项目科学技术要点

- 提出了一种新的图表示学习模型——GSAERU，用于大规模时序图数据上的节点表征学习。
- 提出了一种新的基于图摘要的分布式图神经网络训练框架，称为DSGNN。
- 提出了一种新的基于边分割的图流划分算法——Sketch-DBH，用于处理大规模图流数据下的子图划分问题。

### 论文输入与解析状态

| 项目 | 落盘记录 |
| --- | --- |
| 论文 ID | MF2033k6lC |
| 输入来源 | text_layer |
| 页数 | 90 |
| 参考文献数量 | 91 |
| 解析警告 | — |
| 全文 | [查看文件](<MF2033k6lC/paper-input/full.md>) |

---

## 三、查新点

| 序号 | 中文查新点 | 英文查新点 |
| --- | --- | --- |
| NP-1 | 提出了一种新的图表示学习模型——GSAERU，用于大规模时序图数据上的节点表征学习。 | Proposed a new graph representation learning model - GSAERU, for node representation learning on large-scale sequential graphs. |
| NP-2 | 提出了一种新的基于图摘要的分布式图神经网络训练框架，称为DSGNN。 | Proposed a new distributed graph neural network training framework based on graph summarization, called DSGNN. |
| NP-3 | 提出了一种新的基于边分割的图流划分算法——Sketch-DBH，用于处理大规模图流数据下的子图划分问题。 | Proposed a new edge-segmentation-based graph stream partitioning algorithm - Sketch-DBH, for handling subgraph partitioning problems in large-scale graph stream data. |

最终落盘查新点：3 个。提取、去重及补充的中间轨迹尚无统一的工作区入口；不能由最终点数推断过程已完成。

---

## 四、查新范围要求

检索范围围绕各查新点的中英文表述及技术特征展开。

---

## 五、文献检索范围及检索策略

### 5.1 检索来源

- arxiv.org

### 5.2 检索词

- 图表示学习
- 深度学习
- 图神经网络
- 分布式技术
- graph representation learning
- deep learning
- graph neural network
- distributed

### 5.3 检索式

- **NP-1**
  - `abs:"graph summarization technology" AND abs:graph AND abs:representation AND abs:learning AND abs:model`（arxiv；零命中）
  - `abs:"graph summarization technology" AND (abs:graph AND abs:representation AND abs:learning AND abs:model OR abs:"GSAERU")`（arxiv；零命中）
  - `abs:"graph summarization technology"`（arxiv；零命中）
  - `abs:"graph summarization technology"`（arxiv；零命中）
  - `abs:"graph summarization technology"`（arxiv；零命中）
  - `abs:"graph summarization technology"`（arxiv；零命中）
  - `abs:"graph summarization technology" AND abs:"graph representation learning"`（arxiv；零命中）
  - `abs:"graph summarization technology" AND abs:"graph representation learning"`（arxiv；零命中）
  - `abs:"graph summarization technology"`（arxiv；零命中）
  - `abs:"graph representation learning"`（arxiv；有命中）
  - `abs:"graph representation learning"`（arxiv；有命中）
  - `abs:"graph summarization technology"`（arxiv；零命中）
  - `ti:"large-scale sequential graphs" AND abs:"graph summarization technology"`（arxiv；零命中）
  - `ti:"large-scale sequential graphs" AND abs:"graph summarization technology"`（arxiv；零命中）
  - `ti:"large-scale sequential graphs"`（arxiv；零命中）
  - `ti:"large-scale sequential graphs"`（arxiv；零命中）
  - `ti:"large-scale sequential graphs"`（arxiv；零命中）
  - `ti:"large-scale sequential graphs"`（arxiv；零命中）
- **NP-2**
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework`（arxiv；部分成功）
  - `(abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:Distributed AND abs:GNN AND abs:Training AND abs:Framework)`（arxiv；部分成功）
  - `(abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:Distributed AND abs:GNN AND abs:Training AND abs:Framework)`（arxiv；部分成功）
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework`（arxiv；有命中）
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework`（arxiv；有命中）
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework`（arxiv；有命中）
- **NP-3**
  - `abs:edge-segmentation-based AND abs:graph AND abs:stream AND abs:partitioning AND abs:algorithm`（arxiv；零命中）
  - `(abs:edge-segmentation-based AND abs:graph AND abs:stream AND abs:partitioning AND abs:algorithm OR abs:"Sketch-DBH")`（arxiv；有命中）
  - `(abs:edge-segmentation-based AND abs:graph AND abs:stream AND abs:partitioning AND abs:algorithm OR abs:"Sketch-DBH")`（arxiv；有命中）

### 5.4 任务规划

| 查新点 | 任务 | 类型 | 轮次／attempt | 任务说明 |
| --- | --- | --- | --- | --- |
| NP-1 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |
| NP-1 | T-R2-COUNT-1 | research_gap | 2 | Further investigate the technical features of GSAERU to identify additional evidence that supports the claim of using graph summarization technology to solve the problem of inputting GNN model outputs into RNN models for training.<br>补检系统上下文：{"decision": {"point_id": "NP-1", "action": "research_gap", "reason": "Final valid evidence remains below the configured threshold. The initial literature search did not yield any relevant results, indicating a potential gap in the existing evidence.", "cause_event_ids": ["failure_a3c39de6646142e633922b4c"], "source_id": null, "artifacts": [], "source_record_ids": [], "missing_feature_ids": [], "missing_aspects": [], "review_reason": null, "suggested_focus": [], "max_additional_attempts": 1, "trigger_codes": ["evidence.missing"], "round_number": 1, "coordinator_status": "planned", "planned_task_ids": ["T-R2-COUNT-1"], "planning_origin": "coordinator_llm", "planning_basis_read_ids": [], "coordinator_guidance_ids": ["coordinator.policy/count_gap@1:e60090e2240cf06670d9b59f742905e2b8e13f661810e4b2c1731f5b7c667d2c"]}, "count_gaps": [{"novelty_point_id": "NP-1", "valid_card_count": 0, "required_card_count": 2, "reason": "insufficient_final_evidence"}], "previous_attempts": [{"point_id": "NP-1", "task_id": "T-1", "attempt": 1, "language": "en", "task_type": "literature_search", "description": "针对该查新点执行英文文献检索。", "status": "completed", "produced_card_count": 0, "failure_codes": [], "searches": [{"execution_id": "sex_c4f0e4cc738ba152996eb780", "source_id": "arxiv", "query": "abs:\"graph summarization technology\" AND abs:graph AND abs:representation AND abs:learning AND abs:model", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_5946eca41a87d2bf7b3ed889", "source_id": "arxiv", "query": "abs:\"graph summarization technology\" AND (abs:graph AND abs:representation AND abs:learning AND abs:model OR abs:\"GSAERU\")", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_f40231da7d35fd86d497bf17", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_ae0dbf53d52feaaf0ee3f11c", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_901930b86e2ab4cf9cd0309b", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_d72419250bcf68a0a9fcfa3d", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}]}]} |
| NP-1 | T-R2-QUERY-1 | replan_query | 2 | Replan the query to include more specific terms related to 'graph summarization technology' and 'large-scale sequential graphs' to ensure a broader coverage of relevant literature.<br>补检系统上下文：{"decision": {"point_id": "NP-1", "action": "replan_query", "reason": "The previous query for this novelty point returned zero candidates, indicating that the current search direction may not be comprehensive enough. A different search strategy is needed to explore additional relevant works.", "cause_event_ids": ["failure_5cd01617da33112ff3abec2b"], "source_id": null, "artifacts": [], "source_record_ids": [], "missing_feature_ids": [], "missing_aspects": [], "review_reason": null, "suggested_focus": [], "max_additional_attempts": 1, "trigger_codes": ["coverage.no_match"], "round_number": 1, "coordinator_status": "planned", "planned_task_ids": ["T-R2-QUERY-1"], "planning_origin": "coordinator_llm", "planning_basis_read_ids": [], "coordinator_guidance_ids": ["coordinator.policy/zero_candidates@1:e6e723906d13ba75cfb96316c148cadb38a21db8791dc1bf075f681e7475d51b"]}, "count_gaps": [{"novelty_point_id": "NP-1", "valid_card_count": 0, "required_card_count": 2, "reason": "insufficient_final_evidence"}], "previous_attempts": [{"point_id": "NP-1", "task_id": "T-1", "attempt": 1, "language": "en", "task_type": "literature_search", "description": "针对该查新点执行英文文献检索。", "status": "completed", "produced_card_count": 0, "failure_codes": [], "searches": [{"execution_id": "sex_c4f0e4cc738ba152996eb780", "source_id": "arxiv", "query": "abs:\"graph summarization technology\" AND abs:graph AND abs:representation AND abs:learning AND abs:model", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_5946eca41a87d2bf7b3ed889", "source_id": "arxiv", "query": "abs:\"graph summarization technology\" AND (abs:graph AND abs:representation AND abs:learning AND abs:model OR abs:\"GSAERU\")", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_f40231da7d35fd86d497bf17", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_ae0dbf53d52feaaf0ee3f11c", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_901930b86e2ab4cf9cd0309b", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_d72419250bcf68a0a9fcfa3d", "source_id": "arxiv", "query": "abs:\"graph summarization technology\"", "status": "succeeded", "candidate_count": 0}]}]} |
| NP-2 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |
| NP-2 | T-R2-COUNT-1 | research_gap | 2 | Further investigate the technical features of DSGNN to identify additional evidence that supports the claim of using graph summarization technology to improve the efficiency of distributed GNN training.<br>补检系统上下文：{"decision": {"point_id": "NP-2", "action": "research_gap", "reason": "Final valid evidence remains below the configured threshold. The initial literature search did not yield any relevant results, indicating a potential gap in the existing evidence.", "cause_event_ids": ["failure_c04de0284276ffd97fd5b999"], "source_id": null, "artifacts": [], "source_record_ids": [], "missing_feature_ids": [], "missing_aspects": [], "review_reason": null, "suggested_focus": [], "max_additional_attempts": 1, "trigger_codes": ["evidence.missing"], "round_number": 1, "coordinator_status": "planned", "planned_task_ids": ["T-R2-COUNT-1"], "planning_origin": "coordinator_llm", "planning_basis_read_ids": [], "coordinator_guidance_ids": ["coordinator.policy/count_gap@1:e60090e2240cf06670d9b59f742905e2b8e13f661810e4b2c1731f5b7c667d2c"]}, "count_gaps": [{"novelty_point_id": "NP-2", "valid_card_count": 0, "required_card_count": 2, "reason": "insufficient_final_evidence"}], "previous_attempts": [{"point_id": "NP-2", "task_id": "T-1", "attempt": 1, "language": "en", "task_type": "literature_search", "description": "针对该查新点执行英文文献检索。", "status": "completed", "produced_card_count": 0, "failure_codes": [], "searches": [{"execution_id": "sex_7d4c5c07598347c31fa53d0e", "source_id": "arxiv", "query": "abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework", "status": "partial", "candidate_count": 6}, {"execution_id": "sex_09e37827a277c99e079989cd", "source_id": "arxiv", "query": "(abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:Distributed AND abs:GNN AND abs:Training AND abs:Framework)", "status": "partial", "candidate_count": 5}, {"execution_id": "sex_3bb1ddd0859b94ca5392fd26", "source_id": "arxiv", "query": "(abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:Distributed AND abs:GNN AND abs:Training AND abs:Framework)", "status": "partial", "candidate_count": 5}]}]} |
| NP-3 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |

---

## 六、检索结果

### 6.1 检索概况

记录 2 轮检索计划，生成 1 张原始证据卡；通过 1 张，拒绝 0 张。

### 6.2 相关文献

### card_5665d0bf4908499d6b5a832f · EvoFormer: Learning Dynamic Graph-Level Representations with Structural and Temporal Bias Correction

- 查新点：NP-3
- 主要贡献：EvoFormer introduces a Structure-Aware Transformer Module and an Evolution-Sensitive Temporal Module to address Structural Visit Bias and Abrupt Evolution Blindness in dynamic graph-level representation learning.
- 相关性：0.80
- 置信度：0.90
- 来源：
  - EvoFormer: Learning Dynamic Graph-Level Representations with Structural and Temporal Bias Correction：10.1145/3746252.3761134
    - 引文：To mitigate Structural Visit Bias, EvoFormer introduces a Structure-Aware Transformer Module that incorporates positional encoding based on node structural roles, allowing the model to globally differentiate and accurately represent node structures.
    - 位置：artifact art_ebfbc362d98aa3c6b0b68878 chars:717-966
  - EvoFormer: Learning Dynamic Graph-Level Representations with Structural and Temporal Bias Correction：10.1145/3746252.3761134
    - 引文：To overcome Abrupt Evolution Blindness, EvoFormer employs an Evolution-Sensitive Temporal Module, which explicitly models temporal evolution through a sequential three-step strategy: (I) Random Walk Timestamp Classification, generating initial timestamp-aware graph-level embeddings; (II) Graph-Level Temporal Segmentation, partitioning the graph stream into segments reflecting structurally coherent periods; and (III) Segment-Aware Temporal Self-Attention combined with an Edge Evolution Prediction task, enabling the model to precisely capture segment boundaries and perceive structural evolution trends, effectively adapting to rapid temporal shifts.
    - 位置：artifact art_ebfbc362d98aa3c6b0b68878 chars:967-1621

### 6.3 最终有效证据数量

| 查新点 | 有效证据数 | 状态 |
| --- | ---: | --- |
| NP-1 | 0 | 0 Card |
| NP-2 | 0 | 0 Card |
| NP-3 | 1 | 有证据 |

### 6.4 实际候选审计

| 查新点 | 任务 | 执行状态 | 候选数量 |
| --- | --- | --- | --- |
| NP-1 | T-1 | completed | 0 |
| NP-2 | T-1 | completed | 8 |
| NP-3 | T-1 | completed | 1 |
| NP-1 | T-R2-COUNT-1 | completed | 8 |
| NP-2 | T-R2-COUNT-1 | completed | 8 |
| NP-1 | T-R2-QUERY-1 | completed | 0 |

### 6.5 读取与材料

| 材料目录 | Work 数量 | 来源记录数量 | 材料数量 |
| --- | --- | --- | --- |
| 检索文献 | 18 | 18 | 18 |
| 原论文参考文献 | 0 | 0 | 0 |

材料落盘不代表已阅读；读取位置见评审产物中的 read_citations、reader_observations 和任务审计附件。

### 6.6 证据构建与 Validator

| 阶段 | 数量／状态 |
| --- | --- |
| 原始构建卡 | 1 |
| Validator 通过卡 | 1 |
| 最终 accepted 卡 | 1 |
| 拒绝记录 | 0 |

未记录；不能据此认定已执行或已通过。

---

## 七、评审与完整性检查

评审阶段：complete。

| 查新点 | 状态 | 裁定 | 理由 | 未完成原因 | 读取引用数 | 核验证据数 |
| --- | --- | --- | --- | --- | --- | --- |
| NP-1 | insufficient_evidence | — | — | material_unavailable | 0 | 0 |
| NP-2 | insufficient_evidence | — | — | material_unavailable | 0 | 0 |
| NP-3 | insufficient_evidence | — | — | technical_error | 0 | 0 |

### 单卡及局部评审

| 卡片 | 执行／汇总状态 | 局部裁定 | 理由 |
| --- | --- | --- | --- |
| card_5665d0bf4908499d6b5a832f | completed | — | — |
| card_5665d0bf4908499d6b5a832f | 汇总 failed（局部成果） | — | — |

局部评审不能替代查新点最终裁定；完整特征比较及原文核验引用见评审附件。

### Gate 检查

| Gate | 落盘检查结果 |
| --- | --- |
| Gate A | 通过 |
| Gate B | 尚无独立工作区产物；不能由报告已生成推断检查通过。 |

---

## 八、现场问题与局部恢复

| 节点 | 查新点／任务 | 错误码 | 原因 | 执行状态 | 建议动作 |
| --- | --- | --- | --- | --- | --- |
| — | NP-1；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |
| — | NP-1；— | coverage.no_match | Task-level supplement trigger: coverage.no_match; no novelty verdict is implied. | incomplete | replan_query |
| — | NP-2；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |
| — | NP-3；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |
| — | NP-1；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |
| — | NP-2；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |
| — | NP-3；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |

### 局部恢复决策

未记录；不能据此认定已执行或已通过。

错误和恢复建议不等于已执行纠错或复核通过；构建补正结果见对应任务审计附件。

---

## 九、任务级补检

| 轮次 | 查新点 | 触发码 | 动作 | 规划状态 | 新增任务 | 原因 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | NP-1 | evidence.missing | research_gap | planned | T-R2-COUNT-1 | Final valid evidence remains below the configured threshold. The initial literature search did not yield any relevant results, indicating a potential gap in the existing evidence. |
| 1 | NP-1 | coverage.no_match | replan_query | planned | T-R2-QUERY-1 | The previous query for this novelty point returned zero candidates, indicating that the current search direction may not be comprehensive enough. A different search strategy is needed to explore additional relevant works. |
| 1 | NP-2 | evidence.missing | research_gap | planned | T-R2-COUNT-1 | Final valid evidence remains below the configured threshold. The initial literature search did not yield any relevant results, indicating a potential gap in the existing evidence. |
| 1 | NP-3 | evidence.missing | stop | not_requested | — | Reviewer execution is incomplete; recover its saved card/summary state instead of repeating literature searches. |
| 2 | NP-1 | evidence.missing | stop | not_requested | — | Configured round limit reached; unresolved evidence remains unresolved. |
| 2 | NP-2 | evidence.missing | stop | not_requested | — | Configured round limit reached; unresolved evidence remains unresolved. |
| 2 | NP-3 | evidence.missing | stop | not_requested | — | Configured round limit reached; unresolved evidence remains unresolved. |

补检规划完成不等于任务执行成功；实际执行情况见检索结果和候选审计。

---

## 十、查新结论

### NP-1 · 核验未完成，无法裁定

提出了一种新的图表示学习模型——GSAERU，用于大规模时序图数据上的节点表征学习。

**Reviewer 裁定：** 核验未完成，无法裁定  
**裁定理由：** —  
**置信度：** —  
**报告摘要：** Reviewer 核验未完成，尚不能作出新颖性裁定。  
**未完成原因：** material_unavailable  
**高度相关 Work：**
  - 无

**原文核验证据：**
  - 无

### NP-2 · 核验未完成，无法裁定

提出了一种新的基于图摘要的分布式图神经网络训练框架，称为DSGNN。

**Reviewer 裁定：** 核验未完成，无法裁定  
**裁定理由：** —  
**置信度：** —  
**报告摘要：** Reviewer 核验未完成，尚不能作出新颖性裁定。  
**未完成原因：** material_unavailable  
**高度相关 Work：**
  - 无

**原文核验证据：**
  - 无

### NP-3 · 核验未完成，无法裁定

提出了一种新的基于边分割的图流划分算法——Sketch-DBH，用于处理大规模图流数据下的子图划分问题。

**Reviewer 裁定：** 核验未完成，无法裁定  
**裁定理由：** —  
**置信度：** —  
**报告摘要：** Reviewer 核验未完成，尚不能作出新颖性裁定。  
**未完成原因：** technical_error  
**高度相关 Work：**
  - 无

**原文核验证据：**
  - 无

---

## 十一、报告局限

- NP-1：Reviewer 核验未完成；原因：material_unavailable。
- NP-1：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-2：Reviewer 核验未完成；原因：material_unavailable。
- NP-2：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-3：Reviewer 核验未完成；原因：technical_error。
- NP-3：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-1：已记录检索执行：arxiv 成功 18 次、失败 0 次、未执行 0 项。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- NP-2：已记录检索执行：arxiv 成功 3 次、失败 0 次、未执行 0 项、部分成功 3 次。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- NP-3：已记录检索执行：arxiv 成功 3 次、失败 0 次、未执行 0 项。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- 查新候选已保留供逐点研究；保留不代表其独立贡献已确认。特征保留账本不代表作者贡献已获语义确认，提取范围仍待核查。
- 部分单卡核验已保存，但查新点汇总未完成；局部核验不能替代最终裁定。
- NP-1 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.
- NP-2 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.
- NP-3 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.

---

## 十二、附件及参考信息

### 缺失参考文献

本节点输入未记录该类条目；不代表已核实为无。

### 缺失 Baseline

本节点输入未记录该类条目；不代表已核实为无。

### 引用问题

本节点输入未记录该类条目；不代表已核实为无。

### 被拒绝证据

本节点输入未记录该类条目；不代表已核实为无。

### 检索到的文献

- MAEM: Mitigating Recency Bias via a Replay-Free, Capacity-Controlled Dynamic Mixture of Experts for Class-Incremental Graph Learning：[https://arxiv.org/pdf/2610.20238](https://arxiv.org/pdf/2610.20238)
- Universal Sample-Based Embeddings of Higher-Order Objects：[https://arxiv.org/pdf/2610.19899](https://arxiv.org/pdf/2610.19899)
- Complementary Supervised and Self-Supervised Representations for Out-of-Distribution Graph Learning：[https://arxiv.org/pdf/2610.07628](https://arxiv.org/pdf/2610.07628)
- Sequence-Structured Decision Learning for Branch-and-Bound in Mixed-Integer Linear Programming：[https://arxiv.org/pdf/2610.14835](https://arxiv.org/pdf/2610.14835)
- Transferable Graph Neural Network Surrogates for Molecular Dynamics Across Crystal Symmetries：[https://arxiv.org/pdf/2610.00100](https://arxiv.org/pdf/2610.00100)
- Evolutionary One-Step Generators: Fast and Diverse Sampling for Discrete Design：[https://arxiv.org/pdf/2610.08367](https://arxiv.org/pdf/2610.08367)
- BandPC: Learning Residual-Band Preconditioner Combinations for Flexible Conjugate Gradient Solvers：[https://arxiv.org/pdf/2609.37140](https://arxiv.org/pdf/2609.37140)
- Spectral Reversal: Counteracting Singular Value Bias for Graph Prompting：[https://arxiv.org/pdf/2609.32143](https://arxiv.org/pdf/2609.32143)
- EvoFormer: Learning Dynamic Graph-Level Representations with Structural and Temporal Bias Correction：[https://arxiv.org/pdf/2508.15378](https://arxiv.org/pdf/2508.15378)
- Towards Safer Autonomous Driving in an Open World: A Dual-Process Approach：[https://arxiv.org/pdf/2610.04088](https://arxiv.org/pdf/2610.04088)
- ScaGNN: a Graph Neural Network for Multiple Scattering Simulations：[https://arxiv.org/pdf/2609.37509](https://arxiv.org/pdf/2609.37509)
- GraphBEAT: Graph-based Bucketized Event Aggregation Over Time for Early Risk Stratification of Gastrointestinal Cancers：[https://arxiv.org/pdf/2610.22014](https://arxiv.org/pdf/2610.22014)
- Differentiable Koopman Operator for Contrastive Learning on Dynamic Graphs：[https://arxiv.org/pdf/2610.02990](https://arxiv.org/pdf/2610.02990)
- Extremely Fast and Compact Binary Graph Representations via Randomized Operator Sketching：[https://arxiv.org/pdf/2609.32641](https://arxiv.org/pdf/2609.32641)
- Hyperbolic Graph Representation Learning for Differential Diagnosis on Biomedical Knowledge Graphs：[https://arxiv.org/pdf/2609.18481](https://arxiv.org/pdf/2609.18481)
- Robust and Efficient AI Frameworks for Scalable Material Design and Property Prediction：[https://arxiv.org/pdf/2609.17646](https://arxiv.org/pdf/2609.17646)
- Topology-induced Operators Reveal Complementary Graph Representations without Training：[https://arxiv.org/pdf/2609.08152](https://arxiv.org/pdf/2609.08152)
- Dynamic Heterogeneous Graph Representation Learning: A Survey：[https://arxiv.org/pdf/2609.04779](https://arxiv.org/pdf/2609.04779)

### 阶段产物文件目录

以下链接指向编译时工作目录中的落盘文件。全文及详细审计保留在附件中。
输入文件已通过运行与版本核验。材料版本记录保留本次运行身份及文件摘要。

| 阶段产物 | 工作区相对路径 | 附件状态／入口 |
| --- | --- | --- |
| 论文输入 | paper-input/others/paper.json | [查看文件](<MF2033k6lC/paper-input/others/paper.json>) |
| 解析状态 | paper-input/content-list.json | [查看文件](<MF2033k6lC/paper-input/content-list.json>) |
| 最终提取结果 | novelty-points.json | [查看文件](<MF2033k6lC/novelty-points.json>) |
| 规划与实际检索 | retrieval-plans.json | [查看文件](<MF2033k6lC/retrieval-plans.json>) |
| 候选审计 | candidate-audit.json | [查看文件](<MF2033k6lC/candidate-audit.json>) |
| 检索文献与材料目录 | references/list.json | [查看文件](<MF2033k6lC/references/list.json>) |
| 原论文参考文献目录 | subject_references/list.json | [查看文件](<MF2033k6lC/subject_references/list.json>) |
| 证据构建、Validator 与拒卡 | evidence-cards.json | [查看文件](<MF2033k6lC/evidence-cards.json>) |
| 单卡／查新点评审与 Gate A | novelty-reviews.json | [查看文件](<MF2033k6lC/novelty-reviews.json>) |
| 结论、局部成果、补检与限制 | report.json | [查看文件](<MF2033k6lC/report.json>) |
| 材料版本记录 | report-inputs.json | [查看文件](<MF2033k6lC/report-inputs.json>) |
| 论文全文 | paper-input/full.md | [查看文件](<MF2033k6lC/paper-input/full.md>) |
| 任务读取、构建及补正审计 | research-runs/NP-1/T-1/attempt-1.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-1/T-R2-COUNT-1/attempt-2.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-1/T-R2-QUERY-1/attempt-2.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-2/T-1/attempt-1.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-2/T-R2-COUNT-1/attempt-2.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-3/T-1/attempt-1.json | 文件缺失 |

---

> 本报告由 Novelty Multi-Agent Framework 根据论文内容、检索结果及证据分析自动生成。
> 报告中的查新结论应以实际检索到的公开文献为依据。
