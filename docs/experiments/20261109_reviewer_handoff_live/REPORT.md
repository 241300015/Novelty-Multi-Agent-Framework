# Reviewer 点级交接：真实工作流与 NP-2 补充实验

结论：完整工作流已执行并生成报告；默认交接不展开阅读账本得到真实请求验证。
模型协议遵循和科学判断质量未通过验收，不能把流程的 `SUCCESS` 当作查新结论正确。
原始失败均保留，实验过程中没有改生产代码、补写模型输出或重跑直到成功。

## 环境与范围

- 工作树：`Novelty-Multi-Agent-Framework-reviewer-rebuild`，分支 `refactor/reviewer-rebuild-from-main`。
- 代码：`f727f718` 加上未提交的点级交接实现；实际源码和配置已随 startup 冻结。
- 论文：`examples/MF2033k6lC.pdf`，《面向大规模动态图的图神经网络优化机制研究》。
- 从既有解析后的 PaperInput 开始，复用参考文献初始化缓存；新执行提取、规划、检索、验证、评审、汇总和渲染。
- 所有模型角色只使用本地 `qwen2.5-7b-instruct`，registry 不含付费后备模型。
- 沿用用户对论文派生检索词和参考文献元数据发送至公共服务的明确授权。
- 两轮、并发 4、全流程模型调用上限 210；本次实际没有触及上限。

## 完整工作流结果

运行 ID：`run-bc2f638b6605420c9a2b13c01eae0eb6`。
27 个阶段完成，耗时 555.13 秒；129 次本地模型请求均返回成功。
输入 621,786 tokens、输出 25,929 tokens，合计 647,715 tokens；付费模型调用 0。

首轮研究产出 NP-1 两卡、NP-2 一卡、NP-3 八卡；补检又为 NP-2 产出一卡。
最终通过材料门控的为 NP-1 两卡和 NP-3 六卡，共八卡。两轮共执行 16 次单卡评审，
读取 16 次摘要，48 次菜单请求、16 次严格最终输出；实际 schema 和返回字段检查通过。
原文范围、哈希、Reader 回执和同 Work 引用的机械核验无错误。

| 查新点 | 最终程序结果 | 验收解释 |
| --- | --- | --- |
| NP-1 | `reviewed / novel / 0.9` | 承接契约通过，但六项比较中五项 unknown、一项 partially_supported；不能据此认可最终新颖性裁定 |
| NP-2 | `insufficient_evidence / material_unavailable` | 候选未通过材料筛选，没有执行点级汇总；另补独立实验覆盖原问题 |
| NP-3 | `insufficient_evidence / technical_error` | 两轮点级草稿均在未请求复核时改变单卡比较，程序拒绝 |

Validator 记录了 Crossref HTTP 429，以及 DataCite 无匹配等问题，影响 NP-2 的可用材料。
当前另有 18 条归档的物理检索/获取请求记录，状态均为 200；它们与 Validator 元数据失败记录
覆盖不同，不能据此声称所有外部元数据请求成功。具体失败码见 `results.json`。

## 点级实际请求

以下字符数仅计 `card_reviews`，token 为服务端返回的整个请求输入用量。
四次默认交接均无 `key_quotes / review_evidence / reader_observations / quote / exact_quote`，
没有执行旧的每卡 12,000 字符原文截断。

| 调用 | 查新点 | 交接字符数 | 选中引用数 | 输入 token | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| 0084 | NP-1 第一轮 | 3,764 | 3 | 4,128 | 拒绝：两项 unknown 被改成 partially_supported，未定向复核 |
| 0085 | NP-3 第一轮 | 11,292 | 12 | 7,735 | 拒绝：F1 的 reason 改写，未定向复核 |
| 0127 | NP-1 第二轮 | 4,039 | 3 | 4,309 | 六项比较均原样承接，交接校验通过；点级裁定风险仍在 |
| 0128 | NP-3 第二轮 | 11,032 | 11 | 7,536 | 拒绝：存在未复核的单卡比较改写 |

四次均未提出 `recheck`，所以本次不能验证真实模型能按需发起并完成定向复核。
拒绝的草稿没有作为权威点级裁定进入报告。第一轮 NP-3 的差异只出现在一项理由文字中，
这也暴露了当前“逐字段原样承接”协议的实际成本：改写措辞也会被要求复核，不能把所有拒绝
都解释为已经证实模型科学判断错误。

NP-1 第二轮虽保留了五项 unknown，点级自由文本仍将其组织为 `novel / 0.9`。
这是此前单独保留的 unknown/最终裁定问题，本轮没有解决，也没有修改原模型结果掩盖它。

此外，检查 NP-3 第一张单卡时，模型声称参考文献验证了 GSAERU/DSGNN 的效果；
实际引用的是 *Efficient and Scalable Granular-ball Graph Coarsening Method for Large-scale Graph Node Classification*
的摘要，所读内容讨论的是该文自身的图粗化方法，不能支持这项具体归属。
这属于单卡语义依据问题，正确的交接与哈希校验不能替代它。

## 独立 NP-2 本地模型实验

为覆盖完整工作流未到达的原问题，另外使用上次 NP-2 第二轮 checkpoint 的真实单卡事实，
先核验原材料、回执和冻结 source snapshot，然后以新契约执行一次独立点级调用。
这是新实验，不是绕过旧 checkpoint 配置指纹的恢复，也不算完整工作流成功。

- 交接 2,703 字符，选中引用 6 个；默认原文 0 字符，输入 3,778 tokens。
- 本地模型调用 1 次、耗时 19.03 秒，输出 845 tokens；没有 Reader、新检索或付费模型调用。
- 模型未请求定向复核，F1 转抄了错误证据 ID：

| 含义 | 标识 |
| --- | --- |
| 输入提供 | `rev_ev_f3449d99a94e1634b0ebfce9` |
| 模型输出 | `rev_ev_f3449d9d9a94e1634b0ebfce9` |

校验器以 `summary cites evidence not selected in completed card handoff` 拒绝。
最终为 `insufficient_evidence / technical_error`，没有接受草稿中的 `novel / 0.9`。
这次是引用标识转抄错误，不是旧的原文截断问题。没有自动改正 ID 或重跑获取更好结果。

完整工作流加独立实验共 130 次本地模型调用，付费调用为零。

## 报告呈现与可复核产物

简洁版保留整体总结和逐点逐篇两层，包含 NP-1 两篇、NP-3 六篇文献的评判；
NP-2 明确材料不足。六张未完成点级裁定的单卡结果作为 partial_card_reviews 保留。
报告权威字段与 Reviewer 输出一致；本样例未出现缺失展示编号，但编号关联问题并未独立验收。
报告因此也如实保留了上述不可靠的 NP-1 `novel`，不能当作已验收的科学结论。

- [结构化实验结果与差异](results.json)
- [简洁报告（保留了本次模型结论问题）](../../../outputs/reviewer-handoff-live-20261109/runs/0001/MF2033k6lC/report/MF2033k6lC-concise.md)
- [默认报告](../../../outputs/reviewer-handoff-live-20261109/runs/0001/MF2033k6lC/report/MF2033k6lC-report.md)
- [实际交接请求审计](../../../outputs/reviewer-handoff-live-20261109/handoff-live-audit.json)
- [独立 NP-2 实验结果](../../../outputs/reviewer-handoff-live-20261109/np2-summary/result.json)
- [独立 NP-2 实际请求与响应](../../../outputs/reviewer-handoff-live-20261109/np2-summary/calls.json)

生产源码、上次输入目录和上次 NP-2 checkpoint 的哈希核验均保持不变。
本轮只运行实验和补充记录，没有修改评判逻辑，没有提交或推送。

## 未覆盖及后续决策

- 定向复核请求和格式修复均未实际触发；这两条路径目前仍只有离线测试证据。
- 未调用真实 OpenAI 后端。本轮没有出现上下文超限，但没有修复或单独验收 Researcher 的该问题。
- 两次完整实验的检索材料不同，本轮不能作为同一输入下 token 或语义准确率改进的严格对照。
- 下一步可讨论：让模型选择固定判断/证据标识，由程序回填原字段，降低长 ID 转抄和理由复述错误；
  以及语义校验失败后是否进入明确的定向复核步骤。本轮没有擅自实施这些变更。
- 点级裁定对 unknown 的处理、单卡归属正确性、编号关联，仍各自需要验收。
