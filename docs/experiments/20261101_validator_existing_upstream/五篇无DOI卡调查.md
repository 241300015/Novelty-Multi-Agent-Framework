# 五篇无 DOI 证据卡：DataCite 查询与作者格式调查

> 本摘要随 Validator 任务文档保存；本轮实验原始记录、输入材料和运行产物暂留工作区，不纳入本次提交。文中指向这些材料的链接在未取得本地记录的检出中不可用。

2026-11-02（北京时间）。范围：复核之前真实上游重放中被拒的 ScaGNN、BandPC、SDT-GNN、ABC、2PS；不修改 Validator 代码或既定放行规则，不调用模型。EvoFormer 作为独立个案见本文末尾。

## 原始现象和本地数据

五张卡的历史本地 Work／SourceRecord 有 arXiv ID，但缺 DOI；原 Validator 查询中 DOI.org 不支持无 DOI 题录搜索，DataCite 返回 not_found，Crossref 返回最多五条且标记 truncated，目标不在候选中。原始逐卡记录见 [summary.json](summary.json)。这只说明当时的有界查询未找到可匹配候选，不证明文献不存在。

截至本次检查，官方 arXiv 页面显示各自的 arXiv-issued DOI：

| 文献及官方记录 | DOI |
|---|---|
| [ScaGNN](https://arxiv.org/abs/2609.37509) | `10.48550/arXiv.2609.37509` |
| [BandPC](https://arxiv.org/abs/2609.37140) | `10.48550/arXiv.2609.37140` |
| [SDT-GNN](https://arxiv.org/abs/2404.02300) | `10.48550/arXiv.2404.02300` |
| [ABC](https://arxiv.org/abs/2212.05410) | `10.48550/arXiv.2212.05410` |
| [2PS](https://arxiv.org/abs/2001.07086) | `10.48550/arXiv.2001.07086` |

这些 DOI 来自现时官方页面，不是仅从 ID 猜测，也不能反推历史检索时本地记录曾经包含它们。现行 Validator 构造查询时仅取已存 DOI，不使用 arXiv ID 查证 DOI。[代码](../../../backend/src/novelty_agent_framework/core/metadata_verification.py)

## 真实 DataCite 对照

2026-11-01 17:49 UTC（北京时间 2026-11-02 01:49），对每篇使用当前项目 DataCite Provider 独立执行四组查询，共 20 次：精确 arXiv DOI、标题+年份、标题+本地作者+年份、标题+DataCite 返回的作者写法+年份。全部用 IPv4、8 秒阶段超时、零自动重试。精确查询的标题、作者和年份仍使用历史本地值；其余三组相同标题、年份，仅调整作者条件。以下结果是当次服务返回，不代表长期稳定性。[逐项结果](five_datacite_diagnostics.json)，[执行脚本快照](five_datacite_probe.py)。

| 条件 | 五篇结果 |
|---|---|
| 精确 arXiv DOI | 5/5 found，HTTP 200 |
| 标题+年份，不带作者 | 5/5 found，HTTP 200，各 1 条且未截断 |
| 标题+本地作者+年份 | 5/5 not_found，HTTP 200 |
| 标题+DataCite 题录作者写法+年份 | 5/5 found，HTTP 200，各 1 条 |

当次实验中，去掉作者或换成 DataCite 的作者字符串能召回目标；这支持作者查询表达影响了这五篇的召回，不能泛化到所有姓名或推定其他网络时刻的结果。当前 DataCite 搜索将每位作者用 AND 加入查询条件。[实现](../../../backend/src/novelty_agent_framework/tools/database_search/providers/datacite.py)

精确 DOI 结果的标题及年份与本地一致，但当前 `compare_metadata` 对五篇均报作者不一致。第一个作者举例：

| 文献 | 本地作者 | DataCite 作者 |
|---|---|---|
| ScaGNN | `Rémi Marsal` | `Marsal, Rémi` |
| BandPC | `D. M. Li` | `Li, D. M.` |
| SDT-GNN | `Xin Huang` | `Huang, Xin` |
| ABC | `Junwei Su` | `Su, Junwei` |
| 2PS | `Ruben Mayer` | `Mayer, Ruben` |

当前 DataCite 适配器优先使用 `creator.name`，Validator 只做大小写和空白归一化后逐项严格比较。[适配器](../../../backend/src/novelty_agent_framework/tools/database_search/providers/datacite.py)、[比较](../../../backend/src/novelty_agent_framework/core/metadata_verification.py)。因此本次同时观察到**查询召回问题**与**取得题录后的作者比较问题**；这不是完整 Validator 工作流放行测试，也不自动证明任何不同姓名都是同一人。

SDT-GNN 的 arXiv 页面记录 2024 年预印本与后续版本；会议版的年份可能不同。不得因标题一致就将不同 DOI／版本视为完全相同，也不能用字符串重排处理所有姓名。

## EvoFormer 个案（暂不处理）

历史 arXiv 检索响应已为 `Bo Wang`，本地 Work 与 SourceRecord 保留该值；[arXiv 摘要页](https://arxiv.org/abs/2508.15378)仍显示 `Bo Wang`，同一条目的 [HTML 正文](https://arxiv.org/html/2508.15378)及 [ACM 会议目录](https://www.sigweb.sigweb.hosting.acm.org/toc/cikm25.html)显示 `Bo Wan`，本地提取文本也保留 `Bo Wan`。不能断定正确姓名或说 `.txt` 持久化改写了作者。

原实测中 DOI.org 与 Crossref 对该 DOI 返回 found，但 Validator 将合并候选的作者不一致错误地转成两个来源的 no_match；这是已记录但尚未修复的事件归因缺陷。暂不将其作为本次五篇调查的处理重点。详见[原重放记录](README.md)。
