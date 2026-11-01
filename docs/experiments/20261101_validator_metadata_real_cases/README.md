# Validator 真实文献短摘录案例与受控来源评估

> 本摘要随 Validator 任务文档保存；本轮实验原始记录、输入材料和运行产物暂留工作区，不纳入本次提交。文中指向这些材料的链接在未取得本地记录的检出中不可用。

日期：2026-11-01。**标注者为 AI 代理，依据公开来源独立比对并人工式填写，不是人类审阅；不得称为人类金标准。** 评估目标是材料包及题录身份是否符合下述已冻结的预期，不是判断论文结论是否科学正确。

## 文献与依据

四个独立 Work，三个出版机构、三个期刊、2020/2022/2024 年；包括单作者、多作者和综述。页面、作者、年份和 DOI 由代理对照原出版方与 PubMed/PMC 记录；PMC 常是原文复载，不能当作独立实验或独立人类复核。访问日期 2026-11-01，所用网页可能来自检索服务缓存。短摘录来自原文摘要，保存在同目录 UTF-8 `.txt` 中，未提交全文；`works.json` 给出完整作者、版本、来源、期刊、DOI、摘要位置、许可和摘录文件 SHA-256。

| Work | 出版方原文 / 摘录 | 独立题录核对 | 可确认的篇幅 |
|---|---|---|---|
| Lee 等，2024 | [PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0298351)，摘要最后一句 | [PubMed](https://pubmed.ncbi.nlm.nih.gov/38416772/) | [出版方 PDF](https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0298351&type=printable)，21 页，摘录对应第 1 页 |
| Colavizza 等，2024 | [PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0311493)，摘要最后一段 | [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC11524460/) | [出版方 PDF](https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0311493&type=printable)，19 页，摘录对应第 1 页；出版方另有更正，见 `works.json` |
| Galili 和 Tuller，2020 | [BMC Bioinformatics / Springer Nature](https://link.springer.com/article/10.1186/s12859-020-3479-9)，摘要 Background | [PubMed](https://pubmed.ncbi.nlm.nih.gov/32414319/) | 文章编号 190；本次未取得可核对的 PDF 页数，故不造页数、不运行页数扰动 |
| Noor，2022 | [PeerJ Computer Science 出版方 PDF](https://peerj.com/articles/cs-839.pdf)，摘要 Background | [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC8771759/) | 根据出版方 PDF 页脚及文本记录为 18 页，摘录对应第 1 页；页数尚未独立二次核对；综述文章 |

四篇出版物页面或 PDF 均声明 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；摘录版权归相应作者，出处、作者和原文链接见上表及 `works.json`。仅为评估制作局部摘录；各合成扰动明确标注为本实验修改，不代表原作者或出版方的版本。`article_number` 是电子文章编号，不冒充页码；PDF 页数也不声称是数据库元数据字段。摘录文件的字符位置是本地摘录文件里的偏移，而非原网页或 PDF 的字符偏移；原文位置单列为摘要段落及 PDF 页。

## 真值、受控来源和执行边界

`cases.json` 在本次执行前定义标签、预期、最小行为和扰动类别；`works.json` 定义文献及源信息。`results.json` 保存本次输入哈希、执行 UTC 时间、HEAD、后端/config 工作区 diff 指纹和脚本哈希；它们不能回溯证明更早版本何时冻结。测试实际运行生产 `NoveltyWorkflow.run`、默认入口创建的 `DefaultEvidenceValidator`、Work/Artifact 引用链和元数据组装服务；Researcher/Coordinator 为本地确定性夹具，不调用任何模型。**没有调用 Builder，也没有创建 SourceRecord；Reviewer 服务为 None，只由受控 spy 观察 Reviewer 阶段实际输入，不代表真实模型 Reviewer 已验收。** 两个人工构造的 Provider 响应以出版方及 PubMed/PMC 资料为基础，并非 Crossref/DataCite 的实时响应；为了测试冲突、多个候选、未收录和超时，显式施加合成变化。没有联网，真实 API 可用性和 HTTP 解析能力不由此证明。为绕开当前沙箱线程唤醒限制，实验将 `asyncio.to_thread` 替换为当前事件循环内执行；并发/调度能力也不由此证明。

每篇文献的 baseline、本地/云端/双方缺 DOI 场景保留有效真实摘录；错误 DOI、作者、年份、页数、引文、位置是明确标示的合成输入。字段不一致的 `invalid` 指**提交的材料与题录组合不一致**，不意味着真实论文不存在；页数采用出版方 PDF 页数作为模拟来源提供的已知字段。`source_conflict_or` 和 `unrelated_candidate_or` 注入一个合成候选，正确候选仍在；timeout 和 not_found 分别是模拟故障与模拟来源覆盖缺失。

- **身份核对**单独统计：`valid` 的接受记 TP、拒绝记 FN；`invalid` 的接受记 FP、拒绝记 TN。真假由出版物与预先指定的变化确定，不依据被测系统的输出。
- 错误引文与错误位置属于**本地材料溯源检查**，单独统计预期行为是否符合，不进入身份核对的混淆矩阵。
- 两来源模拟 not_found 和两来源模拟超时分别作为**保守拒绝机制**和**故障处理机制**测试，分别统计预期行为是否符合；均不计入误通过或误拒绝统计。模拟 not_found 不说明真实数据库未收录该文献。
- 身份核对案例级统计把合成变体当独立场景，同一篇反复出现，不能理解为独立文献样本。身份核对 Work 级统计每篇各贡献一个正例 baseline 和一个聚合身份负例单位；任一无效身份扰动被接受，该 Work 的聚合负例为 FP。因此 Work 级正负单位各 4 个，仍是同 4 篇文献；不包含本地溯源和机制测试。

## 运行及结果

```bash
PYTHONDONTWRITEBYTECODE=1 /home/lya3106643285/miniconda3/envs/Novelty/bin/python scripts/evaluate_validator_real_cases.py
```

脚本为每个案例创建临时工作区，并要求 Validator 实际检查 **恰好一张卡**；若 Researcher 失败、零卡或校验阶段未执行，脚本报错，不把零卡算成正确拒绝。每案同时记录来源的实际查询字段并断言预期 DOI（含无 DOI 查询及未对照状态）、预设查询次数、Validator 阶段、必需的错误码、实际 Reviewer 阶段输入及服务关闭；针对超时/未命中分别核对来源状态，冲突/多候选场景核对候选数量、冲突字段和 OR 匹配。任何行为不符均记录于 `behavior_failures` 并使 `policy_expectation_met=false`，不能仅凭拒卡算通过。执行结束后重写 `results.json`，保留每案扰动、预期、结果、拒卡原因、具体事件、来源查询次数、服务关闭状态和逐卡审计。临时运行产物本身不保留；原文摘录和哈希可在案例目录验证；每案分别记录提交给 Validator 的实际定位及未扰动的原始定位。

2026-11-01 按确认的新口径重跑：共 55 案、4 篇 Work，全部 55 个预设行为符合预期。身份核对 39 例：TP=24、FP=0、TN=15、FN=0；本地材料溯源检查 8/8 符合预期；模拟来源未找到的保守拒绝 4/4 符合预期；模拟来源超时的故障处理 4/4 符合预期。身份核对 Work 级正例 baseline 为 TP=4、FN=0，聚合身份负例为 TN=4、FP=0。Galili 因未确认 PDF 总页数少一个页数扰动，所以该 Work 的案例数为 13，其他每篇 14。

本集小、来源与学科不均衡，人工标注未经人类审阅，受控来源复用了已核对的事实；不能据此估计真实世界准确率、误报率、公开 API 可用性或模型生成证据质量。真实公共 API 和真实模型端到端工作流的验收须另行记录；**本次没有调用任何模型**。
