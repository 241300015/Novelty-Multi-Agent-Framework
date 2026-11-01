# 复用历史上游产物：Validator 和本地 Reviewer 直接测试

> 本摘要随 Validator 任务文档保存；本轮实验原始记录、输入材料和运行产物暂留工作区，不纳入本次提交。文中指向这些材料的链接在未取得本地记录的检出中不可用。

日期：2026-11-01。目的：使用已有真实上游运行的 Card、Evidence、任务、查新点与材料，直接测量当前 Validator 和后续 Reviewer，不重新运行论文解析、规划或 Researcher。未修改历史运行中的输入；真实元数据查询仅提交题录字段，本地模型调用仅限 `127.0.0.1:8000` 的 `qwen2.5-7b-instruct`。

后续对五张无 DOI 拒卡的来源、查询和作者格式对照，及暂不处理的 EvoFormer 个案，见[五篇无 DOI 卡调查](五篇无DOI卡调查.md)。

## 样本和方法

- 2026-09-30 [历史真实运行](../20260930_issue_audit_live/README.md)：第 2 轮 Validator 输入，3 张 Card、4 条 Evidence、6 个任务、3 个查新点。历史记录明确指出其中 EvoFormer 卡存在正文贡献归属问题，旧的通过状态不是正确性真值。
- 2026-09-17 [历史真实运行](../20260917_214820_retrieval_repair/report.md)：第 1 轮 Validator 输入，5 张 Card。本次补充该运行，是为了观察真实外部核验放行后的下游路径；两批均使用相应阶段的全部 raw Card，没有筛选后只报告通过卡。
- 每批先调用当前 `DefaultEvidenceValidator.validate` 的本地模式，再对同一输入调用实际 DOI.org、Crossref、DataCite 元数据服务的外部模式。来源 IPv4、8 秒阶段超时、不自动重试；结果受当时网络与有界候选影响。**这是直接调用 Validator，不是调用工作流的 `_validate_evidence` 节点或完整图。**
- 对 09-17 批次实际放行的两张卡，将该批原始 Evidence、任务、查新点和按字节复制的 references 提交当前工作流 `_review_evidence`，使用真实本地 Reviewer。Reviewer 禁止远程模型回退，URL 和网络连接仅允许本机模型地址；未重新执行 Validator 节点或完整图，因此不是自动接线的端到端验收。
- 原输入阶段文件及引用清单的 SHA-256、逐卡题录查询、来源状态、候选和错误事件见 [summary.json](summary.json)。此处保留的 [Validator 脚本](validator_replay.py)和 [Reviewer 脚本](reviewer_replay.py)是执行快照；其 `/tmp` 输出目录固定，重跑前须改成新的隔离路径以免覆盖证据。Reviewer 会向本机模型发送相关论文派生信息和已授权材料，不使用云模型。

## 结果

| 历史批次 | 本地溯源 | 真实元数据核验 | 不同元数据查询 |
|---|---:|---:|---:|
| 09-30 | 3/3 通过 | 0 通过、3 拒绝 | 3 |
| 09-17 | 5/5 通过 | 2 通过、3 拒绝 | 5 |

两批的这些数据是真实上游材料，元数据请求为真实公共服务，既非本目录先前的模拟未收录案例，也不是总体误拒绝率。拒绝不代表文献虚构；历史卡片是否真实、语义是否正确尚未逐项建立独立人工真值。

09-30 中两篇没有 DOI：DOI.org 不支持无 DOI 查询，DataCite 本次未找到，Crossref 返回有界的前 5 个候选且标记截断，未匹配目标。第三篇 EvoFormer 的 DOI.org 和 Crossref 均找到 DOI；本地作者名单为 `Bo Wang`，Crossref 题录对应为 `Bo Wan`，当前严格规则以作者不一致拒卡。仅凭本次来源不能断定哪个名字正确。

09-17 中两张通过：一张由 Crossref 有界结果中的匹配题录支持，另一张由 DOI.org 和 Crossref 的相同 DOI 与题录支持。其余三张无 DOI：DOI.org 不支持，DataCite 本次未找到，Crossref 返回前 5 条且截断，均未匹配。不能将有界候选未匹配推断为全数据库没有该文献。

### 已发现的错误事件归因

09-30 EvoFormer 卡的 DOI.org 已返回 Handle，Crossref 也返回相同 DOI 和标题，但当前错误事件仍分别称这两个来源“未找到该文献”。实际拒绝原因是本地和 Crossref 的作者字段不一致；“来源已找到记录但字段不一致”与“来源未找到”须区分。独立审查定位于 [metadata_verification.py](../../../backend/src/novelty_agent_framework/core/metadata_verification.py)，本次为测试和记录，没有修改生产逻辑。

### 真实本地 Reviewer

09-17 批次两张通过卡均实际进入 Reviewer，对应两个查新点。运行进行了 3 次本地模型 chat 调用，均成功，输入 14,920、输出 2,498 tokens；本地服务的实际费用未知。

- NP-1 的卡片评审返回 `insufficient_evidence / technical_error`：模型引用了未成功读入的 `read_id`（对应读取记录 `returned_chars=0`），没有完成该点汇总。
- NP-3 的卡片与点级评审完成，返回 `reviewed / partially_novel`。这证明非空卡送达且有结构化结果，**不证明语义结论正确**。独立审查发现，该次输出中将提供引文未提及的部分技术特征判为不存在；现有引文不足以支持这一否定，不能据此判断实际文献是否包含这些技术。
- NP-2 没有通过卡，返回 `material_unavailable`。

测试准备中曾先后遇到旧 profile 与当前配置不兼容、缺少报告运行版本记录、缺少本地凭据占位值，以及代理环境与本地连接守卫冲突；这些尝试没有形成有效模型评审。最终使用当前本地 profile，在**测试子进程**移除代理环境变量，未更改系统代理，且仍阻止非本机连接。

## 独立审查和限制

审查 Agent 核对两批输入、阶段文件及 manifest 哈希、查询结果、来源覆盖和错误归因；对 Reviewer 材料目录与历史 references 的 19 个文件逐个核对 SHA-256，确认一致，并核查了本地模型调用和逐卡结果。审查未发现将模拟服务充作真实服务的情况，但指出上述错误事件与 Reviewer 语义证据问题。

详细原始审计和模型轨迹位于 `/tmp/validator-existing-upstream/`，可能随临时目录清理；原始文件包含论文派生内容，未整体复制入仓库。[summary.json](summary.json)保存不含论文全文和模型对话的逐卡摘要。实际查询可能随时间和远端服务变化；样本量小、来自两份历史运行，不能计算有代表性的总体准确率。没有自动修改代码、没有合并或推送。
