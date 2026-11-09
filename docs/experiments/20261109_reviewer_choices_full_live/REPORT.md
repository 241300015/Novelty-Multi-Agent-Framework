# Reviewer 选择协议：样例论文完整真实工作流测试

**流程跑通，证据选项映射验证通过；纯选择式判断继承和科学结论未通过验收。**

本次从样例论文已有 PaperInput 开始，重新执行提取、规划、公共检索、校验、受控单卡评审、
点级汇总、补检、Coordinator 总结和报告渲染。不是单独 NP-2 回放，也不是合成材料。
复用的是论文解析结果和复制后的 subject-reference bootstrap 缓存；不包含重新解析 PDF。

## 运行与产物

- 论文：MF2033k6lC《面向大规模动态图的图神经网络优化机制研究》。
- 代码：实验运行时为 HEAD `b7f065c8` 加工作区中的证据选择协议改动；该实现随后提交为 `052b558a`，运行期间未修改生产代码。
- Run ID：`run-7f571973d8df452d9f81e7348c8226cd`。
- 时间：2026-11-09 08:05:59—08:15:23（北京时间），**564.34 秒，约 9 分 24 秒**。
- 配置：两轮、4 路并发、最多 210 次模型调用、每阶段/任务最多 48 次物理来源请求。
- 唯一模型：本地 `qwen2.5-7b-instruct`，`http://127.0.0.1:8000/v1`；无付费模型 fallback。
- 公共查询沿用用户此前明确授权。没有提交、推送、部署，也没有修改旧实验。

原始产物目录：`outputs/reviewer-choices-live-20261109/`。

- [简洁版查新报告](../../../outputs/reviewer-choices-live-20261109/runs/0001/MF2033k6lC/report/MF2033k6lC-concise.md)
- [默认版查新报告](../../../outputs/reviewer-choices-live-20261109/runs/0001/MF2033k6lC/report/MF2033k6lC-report.md)
- [运行清单](../../../outputs/reviewer-choices-live-20261109/runs/0001/run.json)
- [结构化验证结果](results.json)

报告保留程序实际输出，未手工纠正其中的 `novel` 裁定；它不能作为已验收的科学结论使用。
原始论文、完整请求和大体积日志保存在本地 ignored outputs，本目录仅记录审计结论和哈希。

## 运行结果

| 指标 | 结果 |
| --- | --- |
| 工作流阶段 | 29 个，全部执行成功 |
| 模型调用 | 89 次，全部传输成功；均为本地模型，付费调用 0 |
| Token | 输入 552,891；输出 13,744；合计 566,635 |
| 单卡评审 | 6 次完成，对应 4 张不同卡片；其中 2 张在第二轮再次评审 |
| Reviewer Reader | 6 次成功，全部为摘要 |
| 单卡模型协议 | 18 次菜单选择、6 次 strict 最终草稿；未触发单卡格式修复 |
| 点级汇总 | 4 次正常请求，另有 1 次格式修复；3 次返回有效结果，1 次失败 |
| 定向复核 | 0 次，真实路径未覆盖 |
| 最终参考文献 | 2 篇，分别对应 NP-1、NP-2；NP-3 材料不足 |

本次实际请求的递归 strict schema、返回字段与枚举、Reader 实际回执、原文范围/哈希、
卡片与 Work 引用关系、点级选项映射和报告权威字段绑定审计均无错误。
所有 202 个生产源码/提示/配置文件的哈希与启动前一致；原输入目录 4 个文件也未改变。

点级默认交接没有原文、阅读账本或真实身份字段。四次正常点级调用分别是：

| 调用 | 查新点 | 交接字符数 | 选中证据数 | 输入 tokens |
| --- | --- | ---: | ---: | ---: |
| 0052 | NP-2，首轮 | 2,002 | 4 | 3,738 |
| 0053 | NP-1，首轮 | 5,191 | 5 | 5,436 |
| 0087 | NP-1，第二轮 | 1,836 | 3 | 3,687 |
| 0088 | NP-2，第二轮 | 1,998 | 4 | 3,837 |

字符数仅按 `json.dumps(card_reviews, ensure_ascii=False)` 计算，非整个请求大小。
本轮没有模型上下文超限，也没有证据 ID 拼写错误；不据此宣称过去的上下文问题已修复。
材料均为摘要，本轮也没有验证大量全文片段的压力场景。

## 发现 1：模型仍未稳定遵循纯选择式继承

**三次成功汇总都使用了 `revised_comparisons` 复写三项比较，`inherit_comparisons` 均为空。**
复制后的字段恰好与已注册单卡判断完全相同，因此现有验证器将它们标为 `inherited` 并接受。
这个审计标签表示内容来源匹配，不能等同于“模型选择条目后由 Harness 复制”。

因此必须区分两项结果：

- 短证据、文献、卡片选项经 Harness 解析回填真实 ID：本轮已跑通。
- 模型只选择继承条目、不重写完整判断：本轮完整工作流没有成功遵循；先前独立 NP-2 回放成功不能外推为稳定性保证。

原始输出及实际字段数量在 `choice-audit.json` 和本目录 `results.json` 中保留。

## 发现 2：NP-1 首轮重复判断，格式修复未解决

`0053_local-qwen2.5-7b.json` 同时选择继承 Survey 的 F1，又在 `revised_comparisons` 中重写同一
Work/F1。程序报错：

```text
duplicate feature/Work judgment; select inheritance or a revised judgment, not both
```

`0054_local-qwen2.5-7b.json` 的格式修复保留了重复结构，最终该轮返回
`insufficient_evidence / technical_error`，错误码 `model.output_schema`。
这不是证据 ID 抄错，也不是本次发生了 Reviewer 上下文超限。

修复请求包含旧输出、schema 和错误文本，未带上继承选项到特征/Work 的语义映射。
这是可确认的输入限制；它对本次修复失败的因果贡献尚未做对照验证。
后续可考虑让 Harness 明确约束继承与修订的互斥选择，并为修复提供必要的选项含义；本次测试没有实施这些改动。

## 发现 3：合法引用不等于内容支持，裁定也存在一致性风险

NP-2 单卡事件 `0007.json` 将 F3“通过图摘要技术进行小批量训练”判为 `supported`，
引用 `ev_333ddc95a1b9c3f273021195`。对应来源仅说明 SDT-GNN 提出 SPRING 流式划分算法，
不足以支持上述具体技术断言。F1 的引用也只介绍一种流式分布式训练框架，未支撑理由所称的领导—工作者机制。
此处判断的是实际所引片段的支持范围，不能进一步声称整篇未读全文不存在这些技术。

此外，NP-1 同一 Survey 卡片在首轮将 F2/F3 标为 `unknown`，第二次单卡评审却改为 `supported`。
两个调用留存的 Reader 原文及 Work 相同（事件 `0010.json` 与 `0020.json`）。
这是重复单卡评审的判断变化，不能描述成 Harness 在点级阶段自动把 unknown 升级；
其语义合理性没有被本次机械校验建立。

最终 NP-1、NP-2 都是 `novel / 0.9`，同时各保留三个 `supported`，点级理由主要复述目标论文贡献。
单卡判断、比较对象归属和最终裁定的一致性仍未通过验收。
Coordinator 和 Renderer 的权威字段绑定检查无错误，只能说明它们保留了 Reviewer 输出，不能说明输出正确。

## 检索及元数据限制

首轮生成 NP-1 三张、NP-2 一张卡片；NP-3 首轮没有生成卡片。
第二轮为 NP-3 执行两个补检任务，各产生一张候选卡，但未通过元数据门控。
二次校验后仅 Survey 和 SDT-GNN 两张卡进入最终报告。

物理来源请求归档记录了 28 次请求：27 次 HTTP 200，1 次 arXiv `ConnectError`。
这不代表全部外部访问都成功；元数据失败另在报告失败账本中记录，包括：

| 失败码 | 账本条目数 |
| --- | ---: |
| `validator.metadata.no_match` | 13 |
| `validator.metadata.identity_mismatch` | 60 |
| `provider.rate_limit` | 2 |
| `provider.request.timeout` | 7 |
| `provider.service` | 6 |
| `model.output_schema` | 1 |
| `evidence.missing` | 2 |
| `coverage.no_match` | 1 |

条目数不是独立 HTTP 请求数；同一来源可产生多个身份核验记录。
拒绝原因中包含 DataCite ReadTimeout、HTTP 503 以及外部候选与本地身份不匹配。
NP-3 最终为 `insufficient_evidence / material_unavailable`，不能据此断言没有相关研究。

简洁报告的整体总结和逐点逐篇两层均存在，渲染了文献[1]、[2]，本例没有未关联展示编号。
这并不验收或修复全局文献编号关联问题。

## 复现与边界

实验目录的 `run.py` 仅为标准入口绑定本地模型注册表；实际运行命令为：

```bash
LOCAL_VLLM_API_KEY=EMPTY timeout 1800 \
  /home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  outputs/reviewer-choices-live-20261109/run.py \
  --paper-json outputs/reviewer-choices-live-20261109/input/MF2033k6lC/paper-input/others/paper.json \
  --runs-root outputs/reviewer-choices-live-20261109/runs --run-number 1 \
  --profile outputs/reviewer-choices-live-20261109/profile.json --isolated-config \
  --max-rounds 2 --max-concurrency 4 --max-model-calls 210 --max-physical-provider-requests 48
```

如需另跑，应使用新目录/运行编号；入口拒绝覆盖已有运行。
运行前配置预检通过，仅提示未对全部活动模型实行精确输入 token 准入。

审计脚本为本实验目录下的 `audit.py`、`audit_contracts.py`、`audit_choices.py`，
依次生成来源/回执、schema/报告绑定、选择协议审计结果。
本次未运行额外单元测试，因为未改生产实现；此前 531 passed、3 skipped 是上一轮离线验证记录。
没有真实 OpenAI 或付费 API 验证，没有九场景合成验收，没有新建 Reviewer/subagent，没有实现通用上下文工具。
本次只增加测试产物和记录；已存在的实现改动及未跟踪 runtime 目录保持原状。
