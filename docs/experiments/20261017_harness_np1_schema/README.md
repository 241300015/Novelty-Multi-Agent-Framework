# NP-1：API JSON Schema 选项约束试验（2026-10-17）

请求层约束在本次本地 API 实验中有效：原首轮请求加约束后重放三次，均返回合法编号；完整 NP-1 中 18 次决策全部合法，19 次模型调用产出 1 张 Validator 接受的卡。该卡仍把目标 GSAERU 的贡献归到参考综述上，不能将格式/溯源成功等同于语义正确。

## 改动范围

仅在实验执行脚本加入 `DecisionSchemaClient`，包装现有 API 客户端：

1. 当前菜单非空时，通过 `ModelCallOptions.response_format` 传入 `json_schema`，`option_id.enum` 为本轮有效编号，`strict=true`、`additionalProperties=false`。
2. 每轮重新生成枚举，不沿用旧菜单。
3. 菜单为空、预算收尾或引文纠正时，不加决策 Schema，维持原输出契约。本次未另行给最终卡片加 Schema，以隔离变量。
4. 模型响应仍经过原 Harness 校验；没有将错误说明映射成编号，也没有改提示或纠错逻辑。

没有依赖 vLLM Python 包，没有使用服务商私有 `guided_*` / `structured_outputs` 参数。通过现有 HTTP API 发送 `response_format`；本次服务接受该参数，但未验证其他云端服务的支持情况。

此功能仅通过实验脚本的 `--decision-schema` 显式启用，不是生产 Harness 的全局默认行为。

## 固定条件与审计

- 本地 `http://127.0.0.1:8000/v1/chat/completions`；`qwen2.5-7b-instruct`，temperature=0.3、max_tokens=4096，未指定 seed。
- NP-1、T-1、SearchPlan、候选集合、落盘正文、模型轮次及工具预算保持不变；使用原冻结语料，不访问外部检索服务。
- system/user 提示与先前失败 NP-1 相同；实际首轮请求除新增 `response_format` 外也完全一致。
- 仅运行 Researcher → Builder → Validator；没有 Reviewer。
- 输出：`outputs/harness-np1-schema-20261017/`，没有覆盖前两次 NP-1 记录。
- 三次独立首轮探测，加完整流程 19 次，共 22 次真实模型调用。

审计见 [audit.json](audit.json)。18 次决策均传入与当轮菜单一致的枚举；最后 1 次产卡请求不带该 Schema。所有请求不提供原生工具定义，模型没有返回原生工具调用。

## 首轮请求探测

沿用曾引发“将选项说明当编号”的完整请求，仅增加 JSON Schema。三次均返回 `{"option_id":"v1_0"}`。
此前无 Schema 的三次相同请求重放为 1 次合法、2 次错误说明值。样本数很少，这不是总体错误率估计。

原始请求/响应：[probe-request.json](probe-request.json)、[probe-results.json](probe-results.json)。

## 完整 NP-1 对照

| 指标 | C 组第一次重跑 | 无约束独立复现 | 本次 Schema 约束 |
| --- | ---: | ---: | ---: |
| 模型调用 | 29 | 20 | 19 |
| 无效选项 | 28 | 0 | 0 |
| 实际检索 | 0 | 1 | 1 |
| 摘要读取 | 0 | 8 | 8 |
| 正文读取 | 0 | 0 | 0 |
| 重复读取 | 0 | 0 | 0 |
| Builder 拒绝次数 | 0 | 2 | 0 |
| Builder 产卡 | 0 | 0 | 1 |
| Validator 接受 | 0 | 0 | 1 |
| Validator 通过率 | N/A | N/A | 1/1 |
| 模型调用失败 | 0 | 0 | 0 |
| token | 73649 | 126569 | 121324 |

本次实际流程：一次检索 → 八篇摘要逐篇读取、逐篇选择不获取正文 → 停止检索 → 正文队列为空 → 生成一张卡。没有触发第二/三轮检索或正文阅读。

两次独立完整重跑都没有出现编号错误，因此产卡从 0 到 1 不能单独归因于 Schema。直接支持格式约束效果的证据是首轮重放与每轮请求/返回的枚举核对。

## 卡片内容的限制

接受的卡来自 **A Comprehensive Survey on Graph Summarization with Graph Neural Networks**，两条引文均匹配其已读摘要，位置为 290–532、680–855。

但 `main_contribution` 写的是：

> 提出了基于图神经网络的图总结方法，特别是GSAERU模型。

GSAERU 是任务中的待查新对象；这里把它写进参考综述的贡献描述，所引用摘要不足以支持这种归属。编号输出正确、引文可溯源，并没有消除来源角色混淆。纠错机制按用户要求未调整，本次 Builder 未拒绝草稿，也未触发引文纠正。

## 可复现执行与检查

先在全新输出目录运行 [probe.py](probe.py)，再复制上一轮 `fixture/` 到该输出目录。脚本刻意拒绝覆盖已有目录。

```bash
LOCAL_VLLM_API_KEY=local NOVELTY_EXPERIMENT_PROFILE= \
  /home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  scripts/compare_researcher_harness.py \
  --output outputs/harness-np1-schema-20261017 \
  --arms choice_only --points NP-1 --decision-schema
```

实验包装层测试：`tests/test_harness_schema_experiment.py`，2 passed。覆盖动态菜单枚举、保留提示与原生工具禁用状态、空菜单/预算收尾/纠错调用不残留决策约束。`git diff --check` 通过。未重新运行全量离线测试；生产工作流未修改。
