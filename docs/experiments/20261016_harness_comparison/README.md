# 固定任务：原版、拆分菜单版、纯选项版对照

日期：2026-10-16。本地实验分支：`experiment/harness-choice-only-20261016`，不推送。
本轮范围只有 **Researcher → Builder → Validator**，不重新执行 Extractor、Coordinator、Planner、Reviewer 或 Renderer。

## 三组定义

| 组别 | 模型决策接口 | Builder | 提示来源 |
| --- | --- | --- | --- |
| A：original | 原生工具调用，自由提供参数 | 原版，跨文献混合卡拒绝 | 第一次本地实验的源码/提示快照 |
| B：split_menu | Harness 菜单，仍通过原生选择函数调用 | 来源唯一时拆卡 | 第二次本地实验快照，保留当时的旧工具列表/技能提示 |
| C：choice_only | **不提供原生 tools**，普通 JSON 返回 option_id；Harness 执行 | 与 B 相同的拆卡逻辑 | 独立可配置的 `research/choice_only.md`，不附加 Available tools 或原生工具技能 |

B 保留旧提示是为了如实复现“拆分后”那一版，不能把后来清理过的提示冒充当时版本。
A → B 同时改变读取控制与拆卡，不单独归因于 Builder；B → C 比较的是整个交互接口和配套提示的改变。

## 固定条件

- 固定第一次本地实验的 NP-1/T-1、NP-2/T-1，各自的 NoveltyPoint、ResearchTask、SearchPlan、目标论文身份一致。
- 六次任务调用逐项记录输入哈希；不让模型重新生成查新点或检索计划。
- 固定 16 篇候选和原始检索结果。实验准备阶段对每篇候选获取一次可用全文，冻结 16 份摘要与 16 份正文。
- 三组分别使用相同语料的独立目录副本。检索/全文获取返回冻结结果，Reader 实际读取磁盘文件。
  **这是固定材料、真实模型的对照，不是三次独立在线检索，也不是回放模型答案。**
- 模型均为本地 `qwen2.5-7b-instruct`，32K 上下文；温度均为 0.3、输出上限均为 4096 tokens；轮次/调用/字符预算相同。
- 使用同一个当前 Validator，核验字段、任务归属和落盘溯源。旧源码只替换 Researcher、Harness、读取策略和 Builder。
- 六次任务顺序执行，避免模型服务器并发竞争；不自动补检，不为某组追加预算。
- 同一任务每组仅运行一次；两个任务不足以得出统计显著性结论，也不测 Reviewer 的语义审阅质量。

正文准备与六次执行的数据在 `outputs/harness-comparison-20261016/`。
`fixture/manifest.json` 冻结全部实验材料和 A/B 源码文件哈希；每次启动再检查。
每个任务的 `startup/` 另存当时工作区源码、配置、输入；`actual-input.json` 记录真正采用的历史变体和最终提示。

## 指标口径

- **效果**：生成卡数、Validator 接受卡数、有有效卡的任务数、读取不同区间数、重复区间数。
- **Validator 通过率**：接受卡数 / 提交给 Validator 的卡数；分母为零时是 N/A，不能写成 100% 或 0%。
- **模型调用**：包含探索、预算后的收尾、JSON 修复、引文纠正。另记输入/输出 token。
- **模型错误率**：HTTP/模型调用失败数 / 全部模型调用数。
- **动作错误率**：菜单/读取策略拒绝次数 + 执行接口失败次数，除以模型提出动作的轮数。
  执行前拒绝与实际执行失败分开计数，避免同一次拒绝计算两次；正常预算停止单列，不当作模型 HTTP 错误。
- **工具错误率**：实际交给执行接口的失败次数 / 执行接口调用次数，不含 PRE_TOOL 拒绝。
- Builder 草稿拒绝单独记录，不混进 Validator 拒绝；一张草稿可以拆成多卡，因此不用“输出卡数 / 草稿数”冒充通过率。
- 任务状态 completed 仅表示流程结束；必须结合有效卡数判断是否取得证据。

## 启用纯选项模式

实验配置见 [profile-choice-only.json](profile-choice-only.json)。关键项：

```json
{
  "researcher": {
    "harness": {"guided_reading": true, "text_choices": true},
    "prompt": "research/choice_only"
  }
}
```

该模式使用独立的配置化提示，不加载旧原生工具技能说明。旧模式仍可用于对照，默认配置未全局改写。

## 运行方式

```bash
# 冻结任务与材料，只执行一次；已有 fixture 时拒绝覆盖
LOCAL_VLLM_API_KEY=local NOVELTY_EXPERIMENT_PROFILE= \
  /home/lya3106643285/miniconda3/envs/Novelty/bin/python scripts/compare_researcher_harness.py \
  --prepare --fetch-fulltexts

# 使用本地模型跑三组，共六个固定任务
LOCAL_VLLM_API_KEY=local NOVELTY_EXPERIMENT_PROFILE= \
  /home/lya3106643285/miniconda3/envs/Novelty/bin/python scripts/compare_researcher_harness.py

# 只重新汇总已落盘结果，无模型调用
/home/lya3106643285/miniconda3/envs/Novelty/bin/python scripts/compare_researcher_harness.py --summarize
```

## 验证

纯选项模式测试检查：不发送工具定义、不发送 tool 角色消息、拒绝伪造参数/原生工具调用、
正常执行选项、保留可信 Reader 观察、预算收尾和提示不包含原生工具列表。

全量离线测试：1308 通过，4 个既有失败，6 排除。三个失败依赖此前删除的备份文献，
一个断言已经不存在的 ArxivWebSearchTool 属性，详见 [offline-tests.log](offline-tests.log)。

## 本地模型对照结果

**结论：纯选项版消除了本轮无效工具选择，但没有提高有效卡产出。保留为实验方案，不作为已通过生产验收的改动。**

| 指标（两个固定任务合计） | A 原版 | B 拆分菜单版 | C 纯选项版 |
| --- | ---: | ---: | ---: |
| 模型调用 | 37 | 44 | 29 |
| 无效选择被 Harness 拒绝 | 0 | 34 | 0 |
| 模型提出动作的轮数 | 33 | 42 | 26 |
| 动作错误率 | 1/33 = 3.03% | 34/42 = 80.95% | 0/26 = 0% |
| 模型 HTTP/调用错误率 | 0/37 = 0% | 1/44 = 2.27% | 1/29 = 3.45% |
| 执行接口错误率 | 1/32 = 3.13% | 0/8 = 0% | 0/25 = 0% |
| 实际 Reader 调用 | 28 | 5 | 15 |
| 不同读取区间 | 15 | 5 | 15 |
| 重复读取区间 | 13 | 0 | 0 |
| 读取字符数 | 70,837 | 65,439 | 109,262 |
| Builder 最终产卡 | 1 | 0 | 0 |
| Validator 接受卡 | 1 | 0 | 0 |
| Validator 通过率 | 1/1 = 100% | N/A（未提交卡） | N/A（未提交卡） |
| 有有效卡的任务 | 1/2 | 0/2 | 0/2 |
| 输入 token | 409,641 | 348,436 | 367,376 |
| 输出 token | 10,638 | 1,798 | 670 |
| 总 token | 420,279 | 350,234 | 368,046 |
| 两任务耗时合计 | 331.53 秒 | 107.55 秒 | 109.00 秒 |

A 的执行接口失败是一次全文获取参数超过至多四个来源记录的限制；预算处 PRE_TOOL 停止另计。
B、C 各有一次上下文超限，错误次数相同、调用总数不同，所以错误率分母不同。
C 比 B 少 34.1% 的模型调用，但总 token 高约 5.1%，实际阅读字符更多，不能宣称 token 成本同时下降。

### 逐任务解释

| 组别/任务 | 模型调用 | 接受卡 | 结果解释 |
| --- | ---: | ---: | --- |
| A / NP-1 | 19 | 0 | 三份摘要反复读取；16 次实际读取含 13 次重复，预算收尾返回空卡 |
| A / NP-2 | 18 | 1 | 读取八份摘要和四份正文片段；生成四张草稿，仅一张能绑定真实引文；纠正没有新增合格卡 |
| B / NP-1 | 29 | 0 | 实际召回八篇，但 27 次原生调用被菜单策略拒绝，未读任何材料；最后“没有相关论文”的说法不符合检索事实 |
| B / NP-2 | 15 | 0 | 七次无效选择；摘要加四段正文累计后超出 32K 上下文，尚未生成草稿即返回 partial |
| C / NP-1 | 10 | 0 | 正确选项、无重复读取；摘要加六段正文持续累计，输入加输出预算为 34,634 tokens，超过 32,768 上限 |
| C / NP-2 | 19 | 0 | 正确读完八篇摘要；“重新检索”复用候选仍占数据库调用额度，触及八次预算。收尾把查新点自己的声明当作引文，Builder 与一次纠正均拒绝 |

C / NP-2 被拒绝的“引文”是：

> 提出了一种基于图摘要的分布式图神经网络优化训练算法——DSGNN 模型。

它来自被查新的 NP 声明，而不是成功读取的参考文献。此处拒绝是正确的，不是来源拆分失效。
这次 B/C 没有形成可供“跨文献拆分”挽救的草稿，因此不能用本次零卡反推拆分功能失效。
此前真实混合草稿 1→3 的回放验证是独立证据，不能冒充本次模型产卡结果。

A 唯一通过的卡对应 *Universal Sample-Based Embeddings of Higher-Order Objects*。
通过说明字段和引文溯源满足当前 Validator；没有执行 Reviewer，不能据此认定其相关性评分或查新判断已经审阅通过。

### 协议与输入审计

[audit.json](audit.json) 确认三组每个任务的输入哈希完全一致，模型、温度、输出预算和本地地址一致。
C 全部 29 次模型请求均无 tools 定义、无 Available tools 列表、无 tool 角色消息，模型也没有返回原生工具调用。

### 本轮暴露而未扩展修改的事项

1. 正文持续追加到上下文后，仍可能超出模型输入加输出总窗口；当前异常返回保留读取记录，但没有成卡草稿可保留。
2. 菜单中的“重新检索/选择其他候选”通过数据库接口复用结果，仍消耗数据库工具预算；候选导航与实际检索的预算没有分离。
3. 模型能正确选择动作，不代表能把被查新论文与参考文献的证据角色区分清楚；C / NP-2 的草稿角色混淆需要单独处理。
4. C 的实验同时改变交互协议和配套提示，不能把改进严格归因于删掉某一行 Available tools。

保留上述限制，不在本轮增加上下文压缩、检查点、补检或 Reviewer，以保持实验范围。

完整汇总见 [comparison.json](comparison.json)，逐任务指标与失败原因见 [audit.json](audit.json)。
固定任务原文见 [fixed-requests.json](fixed-requests.json)，全部冻结材料哈希见 [fixture-manifest.json](fixture-manifest.json)。
原始模型请求、Reader 结果、Builder 草稿与 Validator 审计保留在 `outputs/harness-comparison-20261016/runs/<组别>/<NP>/`。

