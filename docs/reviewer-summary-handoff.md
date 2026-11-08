# 单卡判断到点级综合的交接契约

本轮确认的职责：点级 Reviewer 主要综合单卡判断，必要时才定向复核，不默认重新读原文。
本改动不调整 Reader 的读取粒度，不实现通用上下文压缩，也不另建 Reviewer 或审查 subagent。

## 数据边界

| 环节 | 数据与职责 |
| --- | --- |
| Reader | 返回授权范围内的原文和真实读取回执；本轮保持原实现 |
| 单卡持久化结果 | 保留单卡判断、全部阅读账本、读取事实和失败记录 |
| 默认点级输入 | `CardSummaryHandoff`：文献身份、单卡判断、特征比较、选中的证据标识、未知项和局限 |
| 定向复核 | 明确指定卡片、Work、特征、选中证据及疑点后，展示对应的已留存原文 |
| 点级输出 | 仍为 `ReviewerSummaryDraft`；程序验证引用及判断来源，再组成原有领域结果 |

`schemas/reviewer_handoff.py` 单独声明 `card_judgment_v1` 输入模型，拒绝额外字段。
程序通过白名单投影交接，不再把持久化结果过滤几个字段后直接发送。
`review_evidence`、`reader_observations`、读取坐标、原文和旧 `key_quotes` 不在默认交接中。
仅单卡 `highly_relevant_works.evidence_ids` 和 `feature_comparisons.evidence_refs` 选中的
证据进入 `selected_evidence`；读过但未选中的材料仍留在数据面。
单卡执行失败，或结果内部标注技术错误、预算中断、材料不可用时，不提供可承接判断。

单卡判断中的来源范围、理由、补充建议、unknown 和执行局限继续交接。历史点级形状的
单卡结果仍可解析，其 `verdict_reason` 用作支撑说明，不把旧单卡 verdict/confidence
传给点级模型投票。旧数据缺少特征比较时，程序不会替它补造特征判断。

## 承接与定向复核

默认可承接单卡已选引用，不要求相同原文在点级输入中再次展示。
为使程序能够区分组织叙述和改变判断，承接的结构化 `feature_comparisons` 须逐字段保留；
点级 `verdict_reason` 用于跨文献组织叙述。理由改写、关系改变、换证据或改变归属，
都不能静默伪装成同一个单卡判断。

模型可输出如下请求，替代当轮最终草稿：

```json
{
  "recheck": {
    "card_id": "C-1",
    "work_id": "work-1",
    "feature_id": "F3",
    "evidence_ids": ["rev_ev_4"],
    "question": "模型平均同步的描述能否支持图摘要小批量训练这一特征？"
  }
}
```

程序验证目标和引用均属于该卡交接，才返回相应 `quote` 与哈希。这里复用已留存的正式证据，
不重新调用 Reader、不获取新论文、不默认重评整张卡，也不允许请求未选中的整个阅读账本。
原文不足以解决问题时，应保留未知或返回补充请求；新材料获取和进一步范围阅读不在本次实现中。

最终引用仍经过既有 Card/Work/point/feature 绑定验证。新增、修改或互相冲突的特征判断，
还须引用本次针对该 Work 和特征实际展开的证据。其他特征的复核不能授权此项修改。
可以退回不带证据断言的 `unknown + insufficient`，但不能凭未读到内容断言已证实差异。
每次点级调用都有独立复核状态，不能沿用上一调用的复核授权。

运行时审计事件 `summary_targeted_verification` 保存明确目标、疑点、原文哈希及来源；
`summary_result.comparison_origins` 区分 `inherited / rechecked / unresolved`。
这些是运行时审计记录，不是模型自行填写的科学可信度。原文出现及引用合法仍不保证语义判断正确。

## 预算与恢复

点级预算与单卡预算分开，可在 Reviewer 配置中设置：

- `summary_max_calls=6`：包含最终输出、复核决策和格式修复的模型调用总数。
- `summary_max_rechecks=2`：定向复核请求次数。
- `summary_max_recheck_chars=32000`：本次汇总主动请求展开的原文字符总数。
- `summary_timeout_seconds=180`：整个点级过程的共享期限。

复核预算不足时按预算失败返回，不截短原文后仍授权完整证据 ID。
无复核需要时仍只有一次正常总结调用；格式修复也不默认注入账本。
默认交接不再使用每卡 12,000 字符展示预算。以上限制不等于模型 token 准入或通用上下文管理。

持久化领域数据和已有报告不做就地迁移。旧单卡结果可用新契约重新生成点级总结，通常无需重新阅读；
若缺少可靠的特征判断或选中依据，则需要补充评审。仅重渲染旧报告不会产生新总结。
本轮改变了汇总提示和配置指纹，旧 checkpoint 的冻结恢复会正确拒绝配置变化；应在确认来源完整后
从原单卡事实建立新 checkpoint/新实验，不能修改旧指纹或覆盖原结果。
旧固定 S1 实验脚本仍核对其历史 payload，不能用它冒充新协议实验。

Coordinator 的共享 summary 要求和 600 字限制、Renderer 和报告模板均未改动。
新的点级结果会被所有报告模式使用，因此其内容变化不是仅限简洁版。

## 离线验证与实际 NP-2 数据回放

`tests/test_reviewer_summary_handoff.py` 覆盖 7 个长阅读块、6 个选中引用的 NP-2 数据形状，
以及原文体积不影响默认交接、unknown、旧数据、失败卡片、显式复核、跨 Work/feature 拒绝、
未选中账本拒绝、冲突判断、复核状态隔离、预算与格式修复。
现有单卡、checkpoint、严格输出、报告与工作流测试继续运行。

通过 `scripts/audit_reviewer_summary_handoff.py` 对真实样例实验的两份 NP-2 checkpoint
执行离线重建，源文件保持不变，不调用模型或公共检索服务：

| 归档调用 | 原交接字符数 | 新交接字符数 | 原文展示字符数（前→后） | 选中引用 | 留存账本条目 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0050 | 20,344 | 2,125 | 12,000 → 0 | 3 | 4 |
| 0096 | 23,773 | 2,703 | 12,000 → 0 | 6 | 7 |

字符数按 `len(json.dumps(card_reviews, ensure_ascii=False))` 计算，仅比较单卡交接部分，
不是完整模型请求的 token 数。详情和源文件哈希见
[handoff-audit.json](experiments/20261109_reviewer_sample_live/handoff-audit.json)。
回放验证引用交接和原判断承接，不接受旧失败模型草稿，不产生新颖性裁定。

上述离线阶段尚未执行真实本地模型或 OpenAI 后端验证；离线通过不能证明模型能够可靠决定何时复核，
也不能证明单卡判断本身正确。点级自由文本的语义、unknown 升级为最终裁定、文献展示编号关联
仍需独立验收。Researcher 的上下文超限是另一个问题，本轮未修改。

本轮运行结果：

- 扩展回归 402 passed、3 skipped，覆盖 Reviewer、严格输出、checkpoint、Renderer、报告绑定、
  配置加载/工厂及主工作流、TaskResearcher 工作流。三个跳过来自缺少旧原文归档的 benchmark。
- 最后收紧同 Work 多卡引用边界后，交接、点级 Reviewer 和 checkpoint 的 66 项测试全部通过。
- 部分工作流/应用测试在沙箱内停在异步关闭阶段；上述完整扩展回归在沙箱外运行，14.67 秒完成。
- `git diff --check` 通过；新增模块编译检查通过。没有运行全仓库或前端测试，没有调用真实模型。

主要修改位置：`schemas/reviewer_handoff.py` 定义输入，`core/reviewer_summary.py` 处理交接与复核，
`agents/evidence_reviewer.py` 编排模型步骤和最终验证，`prompts/reviewer/summarize_reviews.md`
与内置提示同步；`config/{schemas,factory,loader}.py` 和 Reviewer 示例配置接入独立点级预算。
对应测试与本页离线审计脚本提供可重复验证。

## 后续真实实验

已完成[样例论文完整工作流及独立 NP-2 补充实验](experiments/20261109_reviewer_handoff_live/REPORT.md)。
完整工作流 129 次本地调用、独立 NP-2 一次，付费调用为零。实际请求确认默认交接不再展开原文。
但完整工作流四次点级调用中三次因未经复核改写判断被拒绝；一次承接校验通过却仍暴露
unknown 与最终新颖性裁定之间的既有风险。独立 NP-2 因模型抄错证据 ID 被拒绝。
没有触发定向复核，不能宣称真实复核路径已验收。运行成功不代表模型协议与科学判断通过验收。
