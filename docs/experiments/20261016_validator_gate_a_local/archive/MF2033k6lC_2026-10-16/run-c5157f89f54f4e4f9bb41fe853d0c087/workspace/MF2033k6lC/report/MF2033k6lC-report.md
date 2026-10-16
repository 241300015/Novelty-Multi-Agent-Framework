# 科技查新报告

## 项目信息

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 面向大规模动态图的图神经网络优化机制研究 |
| 英文名称 | — |
| 报告生成时间 | 2026-10-16T17:46:13+08:00 |
| 查新范围 | 图表示学习、深度学习、图神经网络、分布式技术、graph representation learning、deep learning、graph neural network、distributed |

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

- 提出了一种基于图摘要的大规模时序图表示学习方法——GSAERU 模型。
- 提出了一种基于图摘要的分布式图神经网络优化训练算法——DSGNN 模型。

---

## 三、查新点

| 序号 | 中文查新点 | 英文查新点 |
| --- | --- | --- |
| NP-1 | 提出了一种基于图摘要的大规模时序图表示学习方法——GSAERU 模型。 | Proposed a large-scale sequential graph representation learning method based on graph summarization—GSAERU model. |
| NP-2 | 提出了一种基于图摘要的分布式图神经网络优化训练算法——DSGNN 模型。 | Proposed a distributed graph neural network optimization training algorithm based on graph summaries——DSGNN model. |

---

## 四、查新范围要求

检索范围围绕各查新点的中英文表述及技术特征展开。

---

## 五、文献检索范围及检索策略

### 5.1 检索来源

- 未记录有效证据来源

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
  - `abs:"graph summarization" AND abs:"graph autoencoder" AND abs:"recurrent neural network"`（arxiv；零命中）
  - `abs:"graph summarization" AND abs:"graph autoencoder"`（arxiv；有命中）
  - `abs:"graph summarization"`（arxiv；部分成功）
  - `abs:"graph summarization" AND abs:"recurrent neural network"`（arxiv；零命中）
- **NP-2**
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network`（arxiv；有命中）
  - `(abs:distributed AND abs:graph AND abs:neural AND abs:network OR abs:"DGN")`（arxiv；有命中）
  - `(abs:distributed AND abs:graph AND abs:neural AND abs:network OR abs:"DGN")`（arxiv；有命中）
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network`（arxiv；有命中）
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network`（arxiv；有命中）
  - `abs:distributed AND abs:graph AND abs:neural AND abs:network`（arxiv；有命中）

---

## 六、检索结果

### 6.1 检索概况

记录 2 轮检索计划，生成 0 张原始证据卡；通过 0 张，拒绝 0 张。

### 6.2 相关文献

没有证据卡通过质量校验。

### 6.3 最终有效证据数量

| 查新点 | 有效证据数 | 状态 |
| --- | ---: | --- |
| NP-1 | 0 | 0 Card |
| NP-2 | 0 | 0 Card |

---

## 七、查新结论

### NP-1 · 核验未完成，无法裁定

提出了一种基于图摘要的大规模时序图表示学习方法——GSAERU 模型。

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

提出了一种基于图摘要的分布式图神经网络优化训练算法——DSGNN 模型。

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

## 八、报告局限

- NP-1：Reviewer 核验未完成；原因：material_unavailable。
- NP-1：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-2：Reviewer 核验未完成；原因：material_unavailable。
- NP-2：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-1：已记录检索执行：arxiv 成功 3 次、失败 0 次、未执行 0 项、部分成功 1 次。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- NP-2：已记录检索执行：arxiv 成功 6 次、失败 0 次、未执行 0 项。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- 查新候选已保留供逐点研究；保留不代表其独立贡献已确认。特征保留账本不代表作者贡献已获语义确认，提取范围仍待核查。
- 提取器只产生 2 个查新候选；目标数量未达到，后续检索范围可能不完整。
- NP-1 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.
- NP-2 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.

---

## 九、附件及参考信息

### 缺失参考文献

本节点输入未记录该类条目；不代表已核实为无。

### 缺失 Baseline

本节点输入未记录该类条目；不代表已核实为无。

### 引用问题

本节点输入未记录该类条目；不代表已核实为无。

### 被拒绝证据

本节点输入未记录该类条目；不代表已核实为无。

### 检索到的文献

- Graph Additive Models disentangling feature, topology, and interactions：[https://arxiv.org/pdf/2610.20662](https://arxiv.org/pdf/2610.20662)
- MAEM: Mitigating Recency Bias via a Replay-Free, Capacity-Controlled Dynamic Mixture of Experts for Class-Incremental Graph Learning：[https://arxiv.org/pdf/2610.20238](https://arxiv.org/pdf/2610.20238)
- Universal Sample-Based Embeddings of Higher-Order Objects：[https://arxiv.org/pdf/2610.19899](https://arxiv.org/pdf/2610.19899)
- When Missingness Follows the Graph: Beyond Feature Propagation for Missing Features：[https://arxiv.org/pdf/2610.18113](https://arxiv.org/pdf/2610.18113)
- Neural Bayesian Networks for Modular and Scalable Decision-Making Under Uncertainty：[https://arxiv.org/pdf/2610.16902](https://arxiv.org/pdf/2610.16902)
- KARAMEL: Knowledge-Aware Ranking for Multimodal Entity Linking in the Arts via Heterogeneous Graphs：[https://arxiv.org/pdf/2610.16502](https://arxiv.org/pdf/2610.16502)
- Learned Graph Bases for Neural Operators on Power-System Transients：[https://arxiv.org/pdf/2610.15651](https://arxiv.org/pdf/2610.15651)
- An Attention-Guided Graph Neural Network Surrogate for Boundary Integral Computation in Physical Field Reconstruction：[https://arxiv.org/pdf/2610.15590](https://arxiv.org/pdf/2610.15590)
- A Comprehensive Survey on Graph Summarization with Graph Neural Networks：[https://arxiv.org/pdf/2302.06114](https://arxiv.org/pdf/2302.06114)
- Aggregating User Preferences while Ensuring Equity, Diversity, and Inclusion using Graph Summarization：[https://arxiv.org/pdf/2610.07128](https://arxiv.org/pdf/2610.07128)
- Relational Compression: A Framework for Relational Fidelity in Constrained Representations：[https://arxiv.org/pdf/2609.31816](https://arxiv.org/pdf/2609.31816)
- Graph-Adaptive Horseshoe for Compositional Regression：[https://arxiv.org/pdf/2608.17858](https://arxiv.org/pdf/2608.17858)
- CGS: Configurable Graph Summarization with Bounded Neighborhood Loss and Query Support：[https://arxiv.org/pdf/2607.10969](https://arxiv.org/pdf/2607.10969)
- COREKG: Coreset-Guided Personalized Summarization of Knowledge Graphs：[https://arxiv.org/pdf/2605.14900](https://arxiv.org/pdf/2605.14900)
- Spectral Model eXplainer: a chemically-grounded explainability framework for spectral-based machine learning models：[https://arxiv.org/pdf/2605.02684](https://arxiv.org/pdf/2605.02684)
- A Null Model for Mapper Subtype Claims：[https://arxiv.org/pdf/2604.17395](https://arxiv.org/pdf/2604.17395)

---

> 本报告由 Novelty Multi-Agent Framework 根据论文内容、检索结果及证据分析自动生成。
> 报告中的查新结论应以实际检索到的公开文献为依据。
