# 科技查新报告

## 项目信息

| 项目 | 内容 |
| --- | --- |
| 项目名称 | 面向大规模动态图的图神经网络优化机制研究 |
| 英文名称 | — |
| 报告生成时间 | 2026-10-25T17:22:27+00:00 |
| 材料所属时间 | 2026-10-25T17:19:37+00:00 至 2026-10-25T17:22:27+00:00 |
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

- GSAERU模型通过引入图摘要技术将原图映射到固定规模的新图中，解决了GNN模型输出无法直接输入到RNN模型中进行训练的问题。

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
| NP-1 | GSAERU模型通过引入图摘要技术将原图映射到固定规模的新图中，解决了GNN模型输出无法直接输入到RNN模型中进行训练的问题。 | The GSAERU model maps the original graph to a fixed-size new graph through graph summarization technology, solving the problem that the output of the GNN model cannot be directly input into the RNN model for training. |

最终落盘查新点：1 个。提取、去重及补充的中间轨迹尚无统一的工作区入口；不能由最终点数推断过程已完成。

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
  - `ti:"GSAERU模型" AND abs:"图摘要技术" AND abs:"图自编码器"`（arxiv；零命中）
  - `ti:"GSAERU模型" AND abs:"图摘要技术"`（arxiv；零命中）
  - `ti:"GSAERU模型"`（arxiv；零命中）
  - `ti:"GSAERU模型" AND abs:"图自编码器"`（arxiv；零命中）
  - `ti:"GSAERU模型"`（arxiv；零命中）
  - `ti:"GSAERU模型"`（arxiv；零命中）
  - `ti:"GSAERU模型" AND abs:"图摘要技术"`（arxiv；零命中）
  - `ti:"GSAERU模型"`（arxiv；零命中）
  - `ti:"GSAERU模型"`（arxiv；零命中）
  - `ti:"GSAERU模型"`（arxiv；零命中）

### 5.4 任务规划

| 查新点 | 任务 | 类型 | 轮次／attempt | 任务说明 |
| --- | --- | --- | --- | --- |
| NP-1 | T-1 | literature_search | 1 | 针对该查新点执行英文文献检索。 |
| NP-1 | T-R2-QUERY-1 | replan_query | 2 | 调整查询策略，扩大搜索范围，寻找更多关于GSAERU模型及其关键技术的文献。<br>补检系统上下文：{"decision": {"point_id": "NP-1", "action": "replan_query", "reason": "之前的查询未能找到相关文献，需要调整查询策略以覆盖更多可能的相关工作。", "cause_event_ids": ["failure_c8b2ddb10511126f1da6de90"], "source_id": null, "artifacts": [], "source_record_ids": [], "missing_feature_ids": [], "missing_aspects": [], "review_reason": null, "suggested_focus": [], "max_additional_attempts": 1, "trigger_codes": ["coverage.no_match"], "round_number": 1, "coordinator_status": "planned", "planned_task_ids": ["T-R2-QUERY-1"], "planning_origin": "coordinator_llm", "planning_basis_read_ids": [], "coordinator_guidance_ids": ["coordinator.policy/zero_candidates@1:e6e723906d13ba75cfb96316c148cadb38a21db8791dc1bf075f681e7475d51b"]}, "count_gaps": [{"novelty_point_id": "NP-1", "valid_card_count": 0, "required_card_count": 1, "reason": "insufficient_final_evidence"}], "previous_attempts": [{"point_id": "NP-1", "task_id": "T-1", "attempt": 1, "language": "en", "task_type": "literature_search", "description": "针对该查新点执行英文文献检索。", "status": "completed", "produced_card_count": 0, "failure_codes": [], "searches": [{"execution_id": "sex_4f1009bb447d980fb0eb7fca", "source_id": "arxiv", "query": "ti:\"GSAERU模型\" AND abs:\"图摘要技术\" AND abs:\"图自编码器\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_e53032f69d914dd8a4f60496", "source_id": "arxiv", "query": "ti:\"GSAERU模型\" AND abs:\"图摘要技术\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_e6d8ceb8d784e1fe48be8798", "source_id": "arxiv", "query": "ti:\"GSAERU模型\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_d8c8d6f8f8fc78bc614a00ba", "source_id": "arxiv", "query": "ti:\"GSAERU模型\" AND abs:\"图自编码器\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_b31c6d004b5446dab612d5f4", "source_id": "arxiv", "query": "ti:\"GSAERU模型\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_e8ec34029365bc21a6307b7e", "source_id": "arxiv", "query": "ti:\"GSAERU模型\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_e852021fdeb0b61678fb49a9", "source_id": "arxiv", "query": "ti:\"GSAERU模型\" AND abs:\"图摘要技术\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_1a2a262272bbfacd353efff2", "source_id": "arxiv", "query": "ti:\"GSAERU模型\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_5747c78696449f6c0f9d23d8", "source_id": "arxiv", "query": "ti:\"GSAERU模型\"", "status": "succeeded", "candidate_count": 0}, {"execution_id": "sex_1b75f5f2a406972e3ae4682d", "source_id": "arxiv", "query": "ti:\"GSAERU模型\"", "status": "succeeded", "candidate_count": 0}]}]} |

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

### 6.4 实际候选审计

| 查新点 | 任务 | 执行状态 | 候选数量 |
| --- | --- | --- | --- |
| NP-1 | T-1 | completed | 0 |
| NP-1 | T-R2-QUERY-1 | partial | 0 |

### 6.5 读取与材料

| 材料目录 | Work 数量 | 来源记录数量 | 材料数量 |
| --- | --- | --- | --- |
| 检索文献 | 0 | 0 | 0 |
| 原论文参考文献 | 0 | 0 | 0 |

材料落盘不代表已阅读；读取位置见评审产物中的 read_citations、reader_observations 和任务审计附件。

### 6.6 证据构建与 Validator

| 阶段 | 数量／状态 |
| --- | --- |
| 原始构建卡 | 0 |
| Validator 通过卡 | 0 |
| 最终 accepted 卡 | 0 |
| 拒绝记录 | 0 |

未记录；不能据此认定已执行或已通过。

---

## 七、评审与完整性检查

评审阶段：complete。

| 查新点 | 状态 | 裁定 | 理由 | 未完成原因 | 读取引用数 | 核验证据数 |
| --- | --- | --- | --- | --- | --- | --- |
| NP-1 | insufficient_evidence | — | — | material_unavailable | 0 | 0 |

### 单卡及局部评审

未记录；不能据此认定已执行或已通过。

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
| coordinator.plan_supplement | —；— | coordinator.task.type_invalid | Coordinator planning failed for trigger evidence.missing; other triggers remain independent. | incomplete | repair_output |
| researcher.execute | NP-1；T-R2-QUERY-1 | execution.unknown | Task execution did not complete; preserved observations do not establish a scientific conclusion. | incomplete | manual |
| — | NP-1；— | evidence.missing | Task-level supplement trigger: evidence.missing; no novelty verdict is implied. | incomplete | research_gap |

### 局部恢复决策

未记录；不能据此认定已执行或已通过。

错误和恢复建议不等于已执行纠错或复核通过；构建补正结果见对应任务审计附件。

---

## 九、任务级补检

| 轮次 | 查新点 | 触发码 | 动作 | 规划状态 | 新增任务 | 原因 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | NP-1 | evidence.missing | stop | failed | — | evidence.missing 补检规划失败；保留未解决缺口。 |
| 1 | NP-1 | coverage.no_match | replan_query | planned | T-R2-QUERY-1 | 之前的查询未能找到相关文献，需要调整查询策略以覆盖更多可能的相关工作。 |
| 2 | NP-1 | evidence.missing | stop | not_requested | — | Configured round limit reached; unresolved evidence remains unresolved. |

补检规划完成不等于任务执行成功；实际执行情况见检索结果和候选审计。

---

## 十、查新结论

### NP-1 · 核验未完成，无法裁定

GSAERU模型通过引入图摘要技术将原图映射到固定规模的新图中，解决了GNN模型输出无法直接输入到RNN模型中进行训练的问题。

**Reviewer 裁定：** 核验未完成，无法裁定  
**裁定理由：** —  
**置信度：** —  
**报告摘要：** GSAERU模型通过引入图摘要技术将原图映射到固定规模的新图中，解决了GNN模型输出无法直接输入到RNN模型中进行训练的问题。已有文献未涉及此技术细节，但GSAERU模型的具体实现和效果已在多个真实世界的大规模图数据集上进行了验证，显示出良好的性能。尽管如此，当前缺乏直接对比现有技术的文献支持，因此无法确定该模型是否具有绝对新颖性。  
**未完成原因：** material_unavailable  
**高度相关 Work：**
  - 无

**原文核验证据：**
  - 无

---

## 十一、报告局限

- NP-1：Reviewer 核验未完成；原因：material_unavailable。
- NP-1：Reviewer 提出补查请求；原始请求保留在来源 Review，本报告节点未执行补查。
- NP-1：已记录检索执行：arxiv 成功 10 次、失败 0 次、未执行 0 项。以上仅为已记录的执行范围，不代表完整检索或新颖性裁定。
- 查新候选已保留供逐点研究；保留不代表其独立贡献已确认。特征保留账本不代表作者贡献已获语义确认，提取范围仍待核查。
- NP-1 恢复停止：Configured round limit reached; unresolved evidence remains unresolved.

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

未记录带可用链接的检索文献。

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
| 任务读取、构建及补正审计 | research-runs/NP-1/T-1/attempt-1.json | [查看文件](<../research-runs/NP-1/T-1/attempt-1.json>) |
| 任务读取、构建及补正审计 | research-runs/NP-1/T-R2-QUERY-1/attempt-2.json | [查看文件](<../research-runs/NP-1/T-R2-QUERY-1/attempt-2.json>) |

---

> 本报告由 Novelty Multi-Agent Framework 根据论文内容、检索结果及证据分析自动生成。
> 报告中的查新结论应以实际检索到的公开文献为依据。
