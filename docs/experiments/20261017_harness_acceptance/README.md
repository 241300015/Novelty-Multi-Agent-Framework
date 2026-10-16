# Harness 纯选项分支验收（2026-10-17）

**结论：本分支原定的 Harness 交互改造通过本次固定任务验收。** 最终 C 组完成 NP-1、NP-2，36 次决策全部合法，重复读取和接口执行失败均为 0，产出两张通过 Validator 的卡。多轮检索和正文执行通路另以确定性选择、真实磁盘读取验证通过。

该结论限定为这个分支的交互与执行机制，不代表纠错系统或整条查新工作流已完成验收。**贡献归属、比较对象及比较依据问题，归入纠错机制的识别、触发和复核缺口；不把它们算成 Validator 的溯源校验失败。** 本轮只验收，没有改业务实现、扩大 Validator 职责或自动合并 main。

## 版本、条件与范围

- 本地分支：`experiment/harness-choice-only-20261016`；验收基线 `8185a0b3`，功能实现提交 `2301549a`。运行期间 backend 和比较执行器均未改动。
- A：原版源码快照；B：拆分菜单版源码快照；C：当前正式 Harness，动态选项 JSON Schema 已直接接入，不使用实验客户端包装。
- 三组各跑 NP-1、NP-2 一次，共六次任务运行；每个任务的 NP、Task、SearchPlan、预算和模型设置逐项核对相同。
- 模型：本地 `qwen2.5-7b-instruct`，`http://127.0.0.1:8000/v1/chat/completions`，temperature=0.3，max_tokens=4096，32K 上下文；未固定 seed。
- 每项任务 28 轮、24 次工具调用，数据库上限 8 次、Reader 上限 16 次、字符预算 192000；没有因某组失败追加预算。
- 复制原固定语料后校验文件哈希：16 份摘要及 16 份正文。检索与获取返回冻结数据，Reader 实际读磁盘；无云端模型及外部检索请求。
- 运行范围是 **Researcher → Builder → Validator**。没有重新生成 NP/Plan，没有运行 Reviewer 或 Renderer。
- 原始输出：`outputs/harness-acceptance-20261017/`，未覆盖前几轮结果。

一致性和协议检查见 [acceptance-audit.json](acceptance-audit.json)，启动基线见 [acceptance-start.json](acceptance-start.json)。

## 本次真实模型三组对照

本表全部来自本次六次运行，不混入历史最佳结果。

| 指标（两个任务合计） | A 原版 | B 拆分菜单版 | C 最终纯选项版 |
| --- | ---: | ---: | ---: |
| 模型调用 | 38 | 43 | 38 |
| 模型调用失败 | 0 | 0 | 0 |
| 无效选择被拒绝 | 0 | 32 | 0 |
| 接口执行失败 | 1 | 0 | 0 |
| 动作错误率（含接口失败） | 1/33，3.03% | 32/39，82.05% | 0/36，0% |
| Reader 调用 | 27 | 4 | 16 |
| 重复读取区间 | 7 | 0 | 0 |
| 读取字符 | 82424 | 49439 | 24926 |
| Builder 产卡 | 0 | 0 | 2 |
| Validator 接受卡 | 0 | 0 | 2 |
| Validator 通过率 | N/A，未提交卡 | N/A，未提交卡 | 2/2 |
| 有有效卡的任务数 | 0/2 | 0/2 | 2/2 |
| 总 token | 449572 | 334458 | 242380 |
| 耗时合计 | 294.04 秒 | 147.13 秒 | 75.18 秒 |

C 的调用次数与 A 相同、少于 B；本次 token 和耗时更低，但 C 只读摘要、阅读量也较低，不能把成本差异全部归因于交互协议。各组每项任务仅一次，不是总体稳定性或统计显著性证明。三组接口、提示和部分控制逻辑同时不同，不单独归因于删去 Available tools。

A 的一次接口失败是模型一次请求 8 个全文记录，超过参数契约的 4 个上限，在实际获取前被拒绝。模型服务成功返回不等于动作或草稿正确。

逐任务汇总：

| 任务 | 模型调用 | 拒绝的无效选择 | 重复读取 | 有效卡 | 引文纠错调用 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A / NP-1 | 20 | 0 | 6 | 0 | 1，恢复 0 卡 |
| A / NP-2 | 18 | 0 | 1 | 0 | 1，恢复 0 卡 |
| B / NP-1 | 29 | 27 | 0 | 0 | 0，未读材料 |
| B / NP-2 | 14 | 5 | 0 | 0 | 1，恢复 0 卡 |
| C / NP-1 | 19 | 0 | 0 | 1 | 0 |
| C / NP-2 | 19 | 0 | 0 | 1 | 0 |

格式修复计入模型调用，但不混作引文纠错。B / NP-1 耗尽探索轮次后返回 partial；其他任务 completed 也不能单独代表取得了有效证据。

汇总数值见 [comparison.json](comparison.json)，逐任务 Builder 草稿和 Validator 结果见 [cases](cases/)。

## Harness 协议与执行通路

两个 C 任务的实际路径都是：一次检索 → 八篇摘要逐篇读完并选择不获取全文 → 停止检索 → 正文队列为空 → 产卡。全部 38 次请求无原生 tools、无 tool 角色消息、无 Available tools 列表，模型没有返回原生工具调用。36 次选项请求的动态枚举、required、strict 和禁止额外字段均与当前菜单一致，36 次选择全部合法，产卡调用没有残留选项 Schema。

真实模型本次没有选择第二／三轮或正文获取。因此这两部分另做 [check_paths.py](check_paths.py) 的确定性检查：使用同一 NP、Plan、原预算和落盘文件，以脚本选择选项，不调用模型，不计入上述表格。

| 机械路径检查 | NP-1 | NP-2 |
| --- | ---: | ---: |
| 检索策略 | strict+medium+broad → medium+broad → broad | 同左 |
| 空策略调用 | 0 | 0 |
| 摘要读取 | 8 | 8 |
| 选定正文读取 | 4 段，至 EOF | 2 段，至 EOF |
| 正文是否全部在结束检索后读取 | 是 | 是 |
| 重复读取区间 | 0 | 0 |
| Harness 轮次 / 上限 | 24/28 | 22/28 |
| 工具调用 / 上限 | 16/24 | 14/24 |

结果见 [mechanical-paths.json](mechanical-paths.json)。冻结检索器记录传入的策略，但不会真实模拟策略放宽后的召回变化；不能据此断言线上检索效果，也未验证真实模型读长正文时的上下文稳定性。

边界回归 **104 项全部通过**，覆盖非法/旧编号、上下文隔离、空策略与失败检索停止、正文阶段、预算收尾、拆卡和引文纠错审计。日志见 [boundary-tests.log](boundary-tests.log)。本轮未重复整库非 live 回归；此前最终实现的整库结果为 1339 通过及 4 个既有失败。

## 纠错机制：单独记录，不转嫁给 Validator

本轮 C 的两张卡没有 Builder 引文拒绝，因此没有发起引文纠错。人工抽查仍发现需要语义纠错的表述：

- NP-1 的来源是综述，但 main_contribution 写成“提出了……方法”，原创贡献归属需要澄清；differences 中的“其他工作可能……”也没有由两条引用建立比较依据。
- NP-2 的目标是 DSGNN，但差异条目混入 AMIGA 对 GNAN 的改进，未直接建立与 DSGNN 的差异；“DSGNN 没有提及……”属于信息不足，不能自动当成技术差异。

这不是 Reviewer 的自动判定或正式语义质量评分，具体人工记录见 [semantic-audit.json](semantic-audit.json)。Validator 通过仅表示其字段、对象绑定及落盘溯源检查通过。

另外用 [check_correction_boundary.py](check_correction_boundary.py) 回放前一轮已经确认的错误：参考综述的贡献中直接写入目标 GSAERU。保留当时的真实草稿、读取观察，使用当前 Researcher、Builder 和磁盘 Validator；不调用模型。结果仍为 **Builder 拒绝 0、Validator 接受 1、纠错模型调用 0**，见 [correction-boundary.json](correction-boundary.json)。

因此具体缺口是：**现有纠错主要由引文/溯源编译失败触发，语义归属和比较错误尚未进入对应纠错流程。** 对已经触发的纠错，A/B 本次也没有成功恢复有效卡。两种情况必须分别记录为“未识别/未触发”与“触发后未修复”，不能笼统写成 Validator 不完整，更不能仅靠添加错误码宣称纠错已完成。

## 验收判定与复现

- **分支的 Harness 改造目标：本次固定任务验收通过。** 当前选项协议、执行控制、去除重复 I/O、原预算下产卡的实验目标得到支持。
- **纠错机制：仍有独立待处理项。** 本轮没有扩大 Validator 职责，没有修复语义纠错。
- **稳定性边界：**两个固定任务各一次，只验证当前本地服务。多轮及正文为机械通路验证；其他云服务 JSON Schema 支持和长正文真实模型表现未验收。
- 只在本地实验分支保存验收成果；未上传、未合并 main。

复制原 `outputs/harness-comparison-20261016/fixture/` 到一个全新输出目录；执行器与检查脚本拒绝覆盖各自已有案例目录。

```bash
LOCAL_VLLM_API_KEY=local NOVELTY_EXPERIMENT_PROFILE= \
  /home/lya3106643285/miniconda3/envs/Novelty/bin/python -u \
  scripts/compare_researcher_harness.py --output <fresh-output>

NOVELTY_EXPERIMENT_PROFILE= /home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  docs/experiments/20261017_harness_acceptance/check_paths.py <fresh-output>

NOVELTY_EXPERIMENT_PROFILE= /home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  docs/experiments/20261017_harness_acceptance/check_correction_boundary.py <fresh-output>
```

`check_correction_boundary.py` 还需要保留的历史 `outputs/harness-np1-schema-20261017/`；审计脚本 `audit.py` 使用本轮 `acceptance-start.json` 和机械路径结果，不会重新调用模型。
