# 科技查新报告

## 项目信息

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 面向大规模动态图的图神经网络优化机制研究 |
| 英文名称 | — |
| 报告生成时间 | 2026-10-18T17:13:49+00:00 |
| 材料所属时间 | 2026-09-28T07:03:08+00:00 至 2026-09-28T07:09:01+00:00 |
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
- 利用图摘要技术加速模型训练和减少模型占用的计算资源。
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
| 全文 | [查看文件](<../paper-input/full.md>) |

---

## 三、查新点

| 序号 | 中文查新点 | 英文查新点 |
| --- | --- | --- |
| NP-1 | 提出了一种新的图表示学习模型——GSAERU，用于大规模时序图数据上的节点表征学习。 | Proposed a new graph representation learning model, GSAERU, for node representation learning on large-scale sequential graphs. |
| NP-2 | 利用图摘要技术加速模型训练和减少模型占用的计算资源。 | Utilized graph summarization techniques to accelerate model training and reduce computational resource usage. |
| NP-3 | 提出了一种新的基于图摘要的分布式图神经网络训练框架，称为DSGNN。 | Proposed a new distributed graph neural network training framework based on graph summarization, called DSGNN. |
| NP-4 | 提出了一种新的基于边分割的图流划分算法——Sketch-DBH，用于处理大规模图流数据下的子图划分问题。 | Proposed a new edge-segmentation-based graph flow partition algorithm, Sketch-DBH, for subgraph partitioning in large-scale graph stream data scenarios. |

最终落盘查新点：4 个。提取、去重及补充的中间轨迹尚无统一的工作区入口；不能由最终点数推断过程已完成。

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
  - `abs:"graph summarization" AND abs:"graph autoencoder"`（arxiv；有命中）
  - `abs:"graph summarization" AND abs:"graph autoencoder"`（arxiv；有命中）
  - `abs:"graph summarization"`（arxiv；部分成功）
- **NP-2**
  - `abs:"graph summarization technique"`（arxiv；部分成功）
  - `(abs:"graph summarization technique" OR abs:"graph summary" OR abs:"graph abstraction")`（arxiv；部分成功）
  - `(abs:"graph summarization technique" OR abs:"graph summary" OR abs:"graph abstraction")`（arxiv；部分成功）
- **NP-3**
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework`（arxiv；部分成功）
  - `(abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:"DGNNTF" OR abs:"DGNN")`（arxiv；部分成功）
  - `(abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:"DGNNTF" OR abs:"DGNN")`（arxiv；部分成功）
- **NP-4**
  - `abs:edge-segmentation-based AND abs:graph AND abs:flow AND abs:partition AND abs:algorithm`（arxiv；零命中）
  - `(abs:edge-segmentation-based AND abs:graph AND abs:flow AND abs:partition AND abs:algorithm OR abs:edge AND abs:segmentation-based AND abs:graph AND abs:flow AND abs:partitioning)`（arxiv；零命中）
  - `(abs:edge-segmentation-based AND abs:graph AND abs:flow AND abs:partition AND abs:algorithm OR abs:edge AND abs:segmentation-based AND abs:graph AND abs:flow AND abs:partitioning)`（arxiv；零命中）
  - `(abs:edge-segmentation-based AND abs:graph AND abs:flow AND abs:partition AND abs:algorithm OR abs:edge AND abs:segmentation-based AND abs:graph AND abs:flow AND abs:partitioning)`（arxiv；零命中）

### 5.4 任务规划

| 查新点 | 任务 | 类型 | 轮次／attempt | 任务说明 |
| --- | --- | --- | --- | --- |
| NP-1 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |
| NP-2 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |
| NP-3 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |
| NP-4 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |

---

## 六、检索结果

### 6.1 检索概况

记录 1 轮检索计划，生成 3 张原始证据卡；通过 3 张，拒绝 0 张。

### 6.2 相关文献

### card_d78acc7cf4192918effd4d37 · A Comprehensive Survey on Graph Summarization with Graph Neural Networks

- 查新点：NP-1
- 主要贡献：The paper proposes a comprehensive survey on graph summarization techniques that rely on graph neural networks (GNNs). It reviews current state-of-the-art approaches and discusses new research areas like graph reinforcement learning.
- 相关性：0.80
- 置信度：0.80
- 来源：
  - A Comprehensive Survey on Graph Summarization with Graph Neural Networks：https://arxiv.org/pdf/2302.06114
    - 引文：In the past, most graph summarization techniques sought to capture the most important part of a graph statistically. However, today, the high dimensionality and complexity of modern graph data are making deep learning techniques more popular.
    - 位置：artifact art_865984a202a99e3cffb02868 chars:290-532
  - A Comprehensive Survey on Graph Summarization with Graph Neural Networks：https://arxiv.org/pdf/2302.06114
    - 引文：Additionally, the survey provides details of benchmark datasets, evaluation metrics, and open-source tools that are often employed in experimentation settings, along with a detailed comparison, discussion, and takeaways for the research community focused on graph summarization.
    - 位置：artifact art_865984a202a99e3cffb02868 chars:1013-1291

### card_b446b7ae89c522c67fc89f91 · A Comprehensive Survey on Graph Summarization with Graph Neural Networks

- 查新点：NP-2
- 主要贡献：Utilized graph summarization techniques to accelerate model training and reduce computational resource usage.
- 相关性：1.00
- 置信度：1.00
- 来源：
  - A Comprehensive Survey on Graph Summarization with Graph Neural Networks：https://arxiv.org/pdf/2302.06114
    - 引文：In the past, most graph summarization techniques sought to capture the most important part of a graph statistically. However, today, the high dimensionality and complexity of modern graph data are making deep learning techniques more popular. Hence, this paper presents a comprehensive survey of progress in deep learning summarization techniques that rely on graph neural networks (GNNs).
    - 位置：artifact art_865984a202a99e3cffb02868 chars:290-679

### card_82bf841904bfa52e7fe21f23 · Deep Reinforcement Learning on Item-Compatibility Graphs for One-Dimensional Bin Packing

- 查新点：NP-3
- 主要贡献：Proposed a novel end-to-end, size-agnostic graph reinforcement learning framework for 1D-BPP.
- 相关性：1.00
- 置信度：1.00
- 来源：
  - Deep Reinforcement Learning on Item-Compatibility Graphs for One-Dimensional Bin Packing：https://arxiv.org/pdf/2609.25397
    - 引文：In this paper, we present a novel end-to-end, size-agnostic graph reinforcement learning framework for 1D-BPP.
    - 位置：artifact art_2db8220ceff4783f334fa350 chars:417-527

### 6.3 最终有效证据数量

| 查新点 | 有效证据数 | 状态 |
| --- | ---: | --- |
| NP-1 | 1 | 有证据 |
| NP-2 | 1 | 有证据 |
| NP-3 | 1 | 有证据 |
| NP-4 | 0 | 0 Card |

### 6.4 实际候选审计

| 查新点 | 任务 | 执行状态 | 候选数量 |
| --- | --- | --- | --- |
| NP-1 | T-1 | completed | 8 |
| NP-2 | T-1 | completed | 8 |
| NP-3 | T-1 | completed | 8 |
| NP-4 | T-1 | completed | 0 |

### 6.5 读取与材料

| 材料目录 | Work 数量 | 来源记录数量 | 材料数量 |
| --- | --- | --- | --- |
| 检索文献 | 23 | 23 | 23 |
| 原论文参考文献 | 0 | 0 | 0 |

材料落盘不代表已阅读；读取位置见评审产物中的 read_citations、reader_observations 和任务审计附件。

### 6.6 证据构建与 Validator

| 阶段 | 数量／状态 |
| --- | --- |
| 原始构建卡 | 3 |
| Validator 通过卡 | 3 |
| 最终 accepted 卡 | 3 |
| 拒绝记录 | 0 |

未记录；不能据此认定已执行或已通过。

---

## 七、评审与完整性检查

评审阶段：complete。

| 查新点 | 状态 | 裁定 | 理由 | 未完成原因 | 读取引用数 | 核验证据数 |
| --- | --- | --- | --- | --- | --- | --- |
| NP-1 | insufficient_evidence | — | 尚无单篇文献对全部明确技术特征的完整支持，不能形成 not_novel 裁定；已登记局部核验结果保留。 | semantic_evidence | 0 | 0 |
| NP-2 | insufficient_evidence | — | — | technical_error | 0 | 0 |
| NP-3 | insufficient_evidence | — | 尚无单篇文献对全部明确技术特征的完整支持，不能形成 not_novel 裁定；已登记局部核验结果保留。 | semantic_evidence | 0 | 0 |
| NP-4 | insufficient_evidence | — | — | material_unavailable | 0 | 0 |

### 单卡及局部评审

| 卡片 | 执行／汇总状态 | 局部裁定 | 理由 |
| --- | --- | --- | --- |
| card_d78acc7cf4192918effd4d37 | completed | — | — |
| card_b446b7ae89c522c67fc89f91 | completed | — | — |
| card_82bf841904bfa52e7fe21f23 | completed | — | — |
| card_b446b7ae89c522c67fc89f91 | 汇总 failed（局部成果） | — | — |

局部评审不能替代查新点最终裁定；完整特征比较及原文核验引用见评审附件。

### Gate 检查

| Gate | 落盘检查结果 |
| --- | --- |
| Gate A | 未记录；不能据此认定已执行或已通过。 |
| Gate B | 尚无独立工作区产物；不能由报告已生成推断检查通过。 |

---

## 八、现场问题与局部恢复

| 节点 | 查新点／任务 | 错误码 | 原因 | 执行状态 | 建议动作 |
| --- | --- | --- | --- | --- | --- |
| — | NP-1；— | review.missing_features | not_novel requires one Work with a unique supported, cited comparison for every declared feature | incomplete | read_artifact |
| — | NP-1；— | review.missing_features | Reviewer has unresolved semantic evidence needs. | incomplete | read_artifact |
| — | NP-3；— | review.missing_features | not_novel requires one Work with a unique supported, cited comparison for every declared feature | incomplete | read_artifact |
| — | NP-3；— | review.missing_features | Reviewer has unresolved semantic evidence needs. | incomplete | read_artifact |
| — | NP-4；— | evidence.missing | Final evidence count is below the configured threshold. | incomplete | research_gap |

### 局部恢复决策

| 查新点 | 动作 | 原因 |
| --- | --- | --- |
| NP-1 | stop | Configured round limit reached; unresolved evidence remains unresolved. |
| NP-3 | stop | Configured round limit reached; unresolved evidence remains unresolved. |
| NP-4 | stop | Configured round limit reached; unresolved evidence remains unresolved. |

错误和恢复建议不等于已执行纠错或复核通过；构建补正结果见对应任务审计附件。

---

## 九、任务级补检

未记录；不能据此认定已执行或已通过。

补检规划完成不等于任务执行成功；实际执行情况见检索结果和候选审计。

---

## 十、查新结论

### NP-1 · 证据不足，无法裁定

提出了一种新的图表示学习模型——GSAERU，用于大规模时序图数据上的节点表征学习。

**Reviewer 裁定：** 证据不足，无法裁定  
**裁定理由：** 尚无单篇文献对全部明确技术特征的完整支持，不能形成 not_novel 裁定；已登记局部核验结果保留。  
**置信度：** —  
**报告摘要：** 该查新点的关键比较证据不足，尚不能作出新颖性裁定。  
**未完成原因：** semantic_evidence  
**高度相关 Work：**
  - wrk_3341f9be3e3f75887b9da472：The paper provides a comprehensive survey on graph summarization techniques and discusses the use of graph neural networks, which are key components of the proposed GSAERU model. (cards: card_d78acc7cf4192918effd4d37)

**原文核验证据：**
  - 无

### NP-2 · 核验未完成，无法裁定

利用图摘要技术加速模型训练和减少模型占用的计算资源。

**Reviewer 裁定：** 核验未完成，无法裁定  
**裁定理由：** —  
**置信度：** —  
**报告摘要：** Reviewer 核验未完成，尚不能作出新颖性裁定。  
**未完成原因：** technical_error  
**高度相关 Work：**
  - 无

**原文核验证据：**
  - 无

### NP-3 · 证据不足，无法裁定

提出了一种新的基于图摘要的分布式图神经网络训练框架，称为DSGNN。

**Reviewer 裁定：** 证据不足，无法裁定  
**裁定理由：** 尚无单篇文献对全部明确技术特征的完整支持，不能形成 not_novel 裁定；已登记局部核验结果保留。  
**置信度：** —  
**报告摘要：** 该查新点的关键比较证据不足，尚不能作出新颖性裁定。  
**未完成原因：** semantic_evidence  
**高度相关 Work：**
  - wrk_8d87f9c6a6caa9d674d723e0：The work presents a similar architecture but focuses on a different problem domain. (cards: card_82bf841904bfa52e7fe21f23)

**原文核验证据：**
  - 无

### NP-4 · 核验未完成，无法裁定

提出了一种新的基于边分割的图流划分算法——Sketch-DBH，用于处理大规模图流数据下的子图划分问题。

**Reviewer 裁定：** 核验未完成，无法裁定  
**裁定理由：** —  
**置信度：** —  
**报告摘要：** Reviewer 核验未完成，尚不能作出新颖性裁定。  
**未完成原因：** material_unavailable  
**高度相关 Work：**
  - 无

**原文核验证据：**
  - 无

---

## 十一、报告局限

- NP-1：Reviewer 核验未完成；原因：semantic_evidence。
- NP-1：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-2：Reviewer 核验未完成；原因：technical_error。
- NP-2：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-3：Reviewer 核验未完成；原因：semantic_evidence。
- NP-3：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-4：Reviewer 核验未完成；原因：material_unavailable。
- NP-4：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-1：已记录检索执行：arxiv 成功 2 次、失败 0 次、未执行 0 项、部分成功 1 次。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- NP-2：已记录检索执行：arxiv 成功 0 次、失败 0 次、未执行 0 项、部分成功 3 次。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- NP-3：已记录检索执行：arxiv 成功 0 次、失败 0 次、未执行 0 项、部分成功 3 次。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- NP-4：已记录检索执行：arxiv 成功 4 次、失败 0 次、未执行 0 项。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- 查新候选已保留供逐点研究；保留不代表其独立贡献已确认。特征保留账本不代表作者贡献已获语义确认，提取范围仍待核查。
- 部分单卡核验已保存，但查新点汇总未完成；局部核验不能替代最终裁定。
- NP-1 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.
- NP-3 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.
- NP-4 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.

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

- A Comprehensive Survey on Graph Summarization with Graph Neural Networks：[https://arxiv.org/pdf/2302.06114](https://arxiv.org/pdf/2302.06114)
- Graph-Adaptive Horseshoe for Compositional Regression：[https://arxiv.org/pdf/2608.17858](https://arxiv.org/pdf/2608.17858)
- CGS: Configurable Graph Summarization with Bounded Neighborhood Loss and Query Support：[https://arxiv.org/pdf/2607.10969](https://arxiv.org/pdf/2607.10969)
- COREKG: Coreset-Guided Personalized Summarization of Knowledge Graphs：[https://arxiv.org/pdf/2605.14900](https://arxiv.org/pdf/2605.14900)
- Spectral Model eXplainer: a chemically-grounded explainability framework for spectral-based machine learning models：[https://arxiv.org/pdf/2605.02684](https://arxiv.org/pdf/2605.02684)
- A Null Model for Mapper Subtype Claims：[https://arxiv.org/pdf/2604.17395](https://arxiv.org/pdf/2604.17395)
- Explainable Mapper: Charting LLM Embedding Spaces Using Perturbation-Based Explanation and Verification Agents：[https://arxiv.org/pdf/2507.18607](https://arxiv.org/pdf/2507.18607)
- Causal DAG Summarization (Full Version)：[https://arxiv.org/pdf/2504.14937](https://arxiv.org/pdf/2504.14937)
- Structured Reasoning Agentic Framework for Interpretable Critical View of Safety Assessment：[https://arxiv.org/pdf/2609.31524](https://arxiv.org/pdf/2609.31524)
- Improving Question Answering over Knowledge Graphs Using Graph Summarization：[https://arxiv.org/pdf/2203.13570](https://arxiv.org/pdf/2203.13570)
- Cellular-Communication-Level Interpretability for Pathology Foundation Models via Graph Distillation on Microenvironment：[https://arxiv.org/pdf/2609.26073](https://arxiv.org/pdf/2609.26073)
- Scaling R-GCN Training with Graph Summarization：[https://arxiv.org/pdf/2203.02622](https://arxiv.org/pdf/2203.02622)
- Where to Decide: Control-Plane Geometry in Coherence-Limited Quantum Networks：[https://arxiv.org/pdf/2609.23047](https://arxiv.org/pdf/2609.23047)
- kMatrix: A Space Efficient Streaming Graph Summarization Technique：[https://arxiv.org/pdf/2105.05503](https://arxiv.org/pdf/2105.05503)
- ChatDev 2.0: A No-Code Multi-Agent Platform for Developing Everything：[https://arxiv.org/pdf/2609.00714](https://arxiv.org/pdf/2609.00714)
- Deep Reinforcement Learning on Item-Compatibility Graphs for One-Dimensional Bin Packing：[https://arxiv.org/pdf/2609.25397](https://arxiv.org/pdf/2609.25397)
- When Does Advection-Aware Graph Nowcasting Help? A Controlled Study of Distributed Solar Ramp Forecasting with a Self-Supervised Cloud-Motion Estimator：[https://arxiv.org/pdf/2609.30286](https://arxiv.org/pdf/2609.30286)
- The CAMELS-CROCODILE Simulation Suite: A New Cosmology--Astrophysics Playground for Machine Learning：[https://arxiv.org/pdf/2609.23982](https://arxiv.org/pdf/2609.23982)
- ABAI at COLIEE 2026 Task 1: Multi-Stage Retrieval with GraphRAG-Enhanced Meta-Learning, and a Post-Hoc Study of the Cross-Validation-to-Test Gap：[https://arxiv.org/pdf/2609.26237](https://arxiv.org/pdf/2609.26237)
- Droop-Aware Foundation Model Power Flow：[https://arxiv.org/pdf/2609.23723](https://arxiv.org/pdf/2609.23723)
- When Concealed Links Cannot Be Recovered: A Structural Identifiability Bound and Evaluation Pitfalls in Offshore Leak Networks：[https://arxiv.org/pdf/2609.26171](https://arxiv.org/pdf/2609.26171)
- GNN-Based Global CSI Reconstruction for Fronthaul-Limited Distributed MIMO Systems：[https://arxiv.org/pdf/2609.23365](https://arxiv.org/pdf/2609.23365)
- ProtoGuide: Prototype-Driven Guidance for Class-Conditional Graph Generation：[https://arxiv.org/pdf/2609.15239](https://arxiv.org/pdf/2609.15239)

### 阶段产物文件目录

以下链接指向编译时工作目录中的落盘文件。全文及详细审计保留在附件中。
输入文件已通过运行与版本核验。材料版本记录保留本次运行身份及文件摘要。

| 阶段产物 | 工作区相对路径 | 附件状态／入口 |
| --- | --- | --- |
| 论文输入 | paper-input/others/paper.json | [查看文件](<../paper-input/others/paper.json>) |
| 解析状态 | paper-input/content-list.json | [查看文件](<../paper-input/content-list.json>) |
| 最终提取结果 | novelty-points.json | [查看文件](<../novelty-points.json>) |
| 规划与实际检索 | retrieval-plans.json | [查看文件](<../retrieval-plans.json>) |
| 候选审计 | candidate-audit.json | [查看文件](<../candidate-audit.json>) |
| 检索文献与材料目录 | references/list.json | [查看文件](<../references/list.json>) |
| 原论文参考文献目录 | subject_references/list.json | [查看文件](<../subject_references/list.json>) |
| 证据构建、Validator 与拒卡 | evidence-cards.json | [查看文件](<../evidence-cards.json>) |
| 单卡／查新点评审与 Gate A | novelty-reviews.json | [查看文件](<../novelty-reviews.json>) |
| 结论、局部成果、补检与限制 | report.json | [查看文件](<../report.json>) |
| 材料版本记录 | report-inputs.json | [查看文件](<../report-inputs.json>) |
| 论文全文 | paper-input/full.md | [查看文件](<../paper-input/full.md>) |
| 任务读取、构建及补正审计 | research-runs/NP-1/T-1/attempt-1.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-2/T-1/attempt-1.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-3/T-1/attempt-1.json | 文件缺失 |
| 任务读取、构建及补正审计 | research-runs/NP-4/T-1/attempt-1.json | 文件缺失 |

---

> 本报告由 Novelty Multi-Agent Framework 根据论文内容、检索结果及证据分析自动生成。
> 报告中的查新结论应以实际检索到的公开文献为依据。
