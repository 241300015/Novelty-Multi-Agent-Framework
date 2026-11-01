# 真实历史证据卡：实时 DataCite 错误作者对照

> 本摘要随 Validator 任务文档保存；本轮实验原始记录、输入材料和运行产物暂留工作区，不纳入本次提交。文中指向这些材料的链接在未取得本地记录的检出中不可用。

日期：2026-11-02（北京时间）。使用上一轮的五篇真实历史文献及原始 EvidenceCard、Evidence、引文、位置、任务和查新点。样本和预期在运行前固定于 `plan.json`；原始阶段输入和引用清单的 SHA-256、每次请求及生产 Validator 完整审计保存在 `results.json`。实验程序为 `run.py`。

## 方法

每篇建立隔离副本并运行两次生产 `DefaultEvidenceValidator`，仅启用真实 DataCite Provider；不使用回放、模拟 Provider 或缓存的跨案例响应。正反例均将上轮已对照官方 arXiv 页面确认的 arXiv DOI 加入副本的卡片来源及本地 Work，以强制精确 DOI 实时查询。历史原始材料没有 DOI，未改变原始文件，也不声称原始记录有 DOI。负例额外把副本中 Work 和对应 SourceRecord 的首位作者一致地替换成另一篇样本文献的真实作者；其他作者和证据材料不变。这是人为构造的错误作者负例，不是真实出版物的错误标注。

每个案例均为一张卡，一次 Validator 调用；同一卡多个来源项若形成相同查询由 Validator 内部缓存，因此每次记录到一个 DataCite HTTP 请求。DataCite 配置为 IPv4、8 秒超时、零自动重试。十次均返回 HTTP 200、目标 DOI 题录及 `found`。每个负例的每个来源项均记录 `identity_mismatch`，其字段包含 `authors`；因此拒绝不是因为未收到真实题录。正负例使用同一篇文献，不能当作十篇独立文献。

| 文献 | 原首位作者 | 负例替换为 | 正例 | 负例 |
|---|---|---|---|---|
| ScaGNN | Rémi Marsal | D. M. Li | 接受 | 拒绝 |
| BandPC | D. M. Li | Xin Huang | 接受 | 拒绝 |
| SDT-GNN | Xin Huang | Junwei Su | 接受 | 拒绝 |
| ABC | Junwei Su | Ruben Mayer | 接受 | 拒绝 |
| 2PS | Ruben Mayer | Rémi Marsal | 接受 | 拒绝 |

正例 TP=5、FN=0；负例 TN=5、FP=0。该组准确率 10/10=100%，误拒率 0/5=0%，误放行率 0/5=0%。分母仅为本组已定义的正例或负例，不是总体风险估计。负例只覆盖完全替换首位作者的明显错误；不覆盖姓名格式歧义、近似拼写、机构作者、复姓或不同作者顺序，也未测试多来源错误合并。样本选自历史五篇、非随机；样本很小、无人工真值审阅，不代表实际场景总体误放行率。

审计中的 `validator.metadata.no_match` 与 `identity_mismatch` 可能同时出现；本实验每次 DataCite 都已返回目标 DOI 记录，不能把该事件解释为 DataCite 未找到 DOI。引用的实验不涉及 Reviewer。
