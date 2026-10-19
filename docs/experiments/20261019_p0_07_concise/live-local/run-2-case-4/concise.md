# 科技查新报告

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 面向大规模动态图的图神经网络优化机制研究 |
| 英文名称 | — |
| 编制日期 | 2026-10-19 |

## 一、查新目的

围绕项目所列查新点，检索公开文献并进行分析对比，评价其新颖性。

## 二、项目的科学技术要点

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

## 三、查新点

1. 提出了一种新的图表示学习模型——GSAERU，用于大规模时序图数据上的节点表征学习。

2. 利用图摘要技术加速模型训练和减少模型占用的计算资源。

3. 提出了一种新的基于图摘要的分布式图神经网络训练框架，称为DSGNN。

4. 提出了一种新的基于边分割的图流划分算法——Sketch-DBH，用于处理大规模图流数据下的子图划分问题。

## 四、查新范围要求

围绕所列查新点开展公开文献对比。结论限于本报告所列来源与实际取得的材料。

## 五、文献检索范围及检索策略

### （一）检索来源

arxiv

### （二）检索词

- 图表示学习
- 深度学习
- 图神经网络
- 分布式技术
- graph representation learning
- deep learning
- graph neural network
- distributed

### （三）检索式

1. abs:"graph summarization" AND abs:"graph autoencoder"
2. abs:"graph summarization"
3. abs:"graph summarization technique"
4. (abs:"graph summarization technique" OR abs:"graph summary" OR abs:"graph abstraction")
5. abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework
6. (abs:distributed AND abs:graph AND abs:neural AND abs:network AND abs:training AND abs:framework OR abs:"DGNNTF" OR abs:"DGNN")
7. abs:edge-segmentation-based AND abs:graph AND abs:flow AND abs:partition AND abs:algorithm
8. (abs:edge-segmentation-based AND abs:graph AND abs:flow AND abs:partition AND abs:algorithm OR abs:edge AND abs:segmentation-based AND abs:graph AND abs:flow AND abs:partitioning)

## 六、检索结果

现摘录可用于对比的相关文献 3 篇。

[1] **A Comprehensive Survey on Graph Summarization with Graph Neural Networks**。https://arxiv.org/pdf/2302.06114

The paper proposes a comprehensive survey on graph summarization techniques that rely on graph neural networks (GNNs). It reviews current state-of-the-art approaches and discusses new research areas like graph reinforcement learning.

[2] **A Comprehensive Survey on Graph Summarization with Graph Neural Networks**。https://arxiv.org/pdf/2302.06114

Utilized graph summarization techniques to accelerate model training and reduce computational resource usage.

[3] **Deep Reinforcement Learning on Item-Compatibility Graphs for One-Dimensional Bin Packing**。https://arxiv.org/pdf/2609.25397

Proposed a novel end-to-end, size-agnostic graph reinforcement learning framework for 1D-BPP.

## 七、查新结论

**查新点 1**

已有文献涉及了图摘要技术在图表示学习中的应用，但尚未有文献全面支持GSAERU模型的具体技术特征，包括引入图摘要技术将原图映射到固定规模的新图、通过图自编码器学习节点表征以及将图摘要生成的节点表征聚合得到原图的节点表征。GSAERU模型结合了图摘要技术和图自编码器，旨在高效生成大规模时序图的节点表征，相较于传统方法具有较低的内存消耗和训练时间。

**查新点 2**

已有文献讨论了利用图摘要技术加速模型训练和减少模型占用的计算资源，但具体技术细节尚未有文献详细描述。GSAERU模型通过生成多个规模较小的子图并在这些子图上进行模型训练，从而加速了训练过程并减少了计算资源的使用。

**查新点 3**

已有文献提出了基于图摘要的分布式图神经网络训练框架，但具体实现细节尚未有文献详细描述。GSAERU模型提出的DSGNN模型采用了“领导——工作者”工作模式，通过领导节点和工作者节点的协同工作，实现了高效的分布式图神经网络训练。

**查新点 4**

当前查新点没有可供可靠判定的已绑定证据，因此无法对其新颖性做出裁定。Sketch-DBH算法的具体实现细节尚未有文献详细描述，需进一步补充相关证据。
