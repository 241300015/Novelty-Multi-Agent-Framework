# 科技查新报告

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 面向大规模动态图的图神经网络优化机制研究 |
| 英文名称 | — |
| 编制日期 | 2026-10-25 |

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

1. 提出了一种基于图摘要技术的图表示学习模型GSAERU，解决了现有图神经网络模型无法处理动态图的问题。

2. 提出了一种基于图摘要的分布式图神经网络训练框架DSGNN，提高了大规模图神经网络模型的训练效率。

3. 通过实验验证了GSAERU和DSGNN模型的有效性和优越性，与现有算法相比，GSAERU模型的内存消耗和训练时间显著减少，DSGNN模型的训练速度明显加快。

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

1. "graph summarization" AND "graph autoencoder"
2. ("graph summarization" OR "graph condensation" OR "graph coarsening") AND ("graph autoencoder" OR "GAE")
3. ("graph summarization" OR "graph condensation" OR "graph coarsening")
4. "distributed graph neural network training framework" AND "leader-worker mode"
5. ("distributed graph neural network training framework" OR "D-GNN training framework") AND ("leader-worker mode" OR "leader worker mode")
6. ("distributed graph neural network training framework" OR "D-GNN training framework")
7. "GSAERU model" AND "DSGNN model"
8. ("GSAERU model" OR "GSAERU") AND ("DSGNN model" OR "DSGNN")
9. ("GSAERU model" OR "GSAERU")

## 六、检索结果

现摘录可用于对比的相关文献 2 篇。

[1] **DSGNN: A Dual-View Supergrid-Aware Graph Neural Network for Regional Air Quality Estimation**。https://arxiv.org/pdf/2404.01975

提出了Dual-view Supergrid-aware Graph Neural Network (DSGNN)模型，用于区域空气质量估计，能够从卫星衍生的气溶胶光学厚度（AOD）和气象学两个视角建模远距离网格区域的空间依赖关系。

[2] **DSGNN: A Dual-View Supergrid-Aware Graph Neural Network for Regional Air Quality Estimation**。https://arxiv.org/pdf/2404.01975

提出了DSGNN模型，可以从双视图（即卫星衍生的气溶胶光学厚度（AOD）和气象学）建模远距离网格区域的空间依赖关系，通过引入双视图超网格学习模块和双视图隐含相关编码模块来学习成对超网格之间的相关性，并通过引入双视图消息传递网络实现超网格图和图像上的信息交互。

## 七、查新结论

**查新点 1：核验未完成，无法裁定。**

现有证据不足以支持判断。

**查新点 2：核验未完成，无法裁定。**

现有证据不足以支持判断。

**查新点 3**

本文通过实验验证了GSAERU和DSGNN模型的有效性和优越性，与现有算法相比，GSAERU模型的内存消耗和训练时间显著减少，DSGNN模型的训练速度明显加快。已有文献未涉及具体实验细节，因此无法直接支持或反驳GSAERU和DSGNN模型的有效性。然而，GSAERU模型在摘要中明确指出其性能优于现有算法。

## 八、备注

- 候选通过模型审核不代表已独立验证整篇论文的贡献覆盖，提取范围仍待核查。
