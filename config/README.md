# 可复现实验配置

默认使用 `profiles/local-workflow.json`：五个查新角色、标题提取及通用客户端首先调用本地 Qwen。模型选用顺序固定为 **本地 → 硅基流动 → 私人 DeepSeek 账户**。本地服务地址为 `http://127.0.0.1:8000/v1`；无鉴权服务使用 `LOCAL_VLLM_API_KEY=local`，有鉴权时填写实际本地密钥。

连接失败、传输超时、缺少凭据、鉴权/额度限制或服务端错误触发下一档切换询问。网页任务显示“允许切换”和“拒绝并中断”，命令行显示 `[y/N]`；只有明确允许才会向下一服务发送论文内容。同一任务对同一目标的同意可复用，私人账户仍需另一次同意；拒绝后中断任务。无人值守入口无法确认时中断，绝不因等待或缺少输入而自动同意。格式错误、上下文拒绝、预算耗尽不会触发付费切换。运行级预算与取消仍有效。

硅基流动使用 `profiles/cloud-workflow.json` / `SILICONFLOW_API_KEY`，私人账户使用下述配置。显式 profile、角色环境覆盖及 CLI override 仍表示主动选择对应起始模型；正常使用保留本地默认配置。模型各自读取 `api_key_env`，不会借用其他账户的通用密钥。

默认 PDF 使用本地 MinerU，关闭硅基流动 OCR 兜底（`ocr_fallback_enabled=false`），因为现有本地 Qwen 没有声明视觉能力；需要云端 OCR 时显式选择云端 profile 或启用该开关。模型切换只替换文本调用，不会把图片发送给不支持视觉的模型。历史专用厂商探针、live 测试与冻结实验仍显式指定其被测模型，不属于默认工作流。

私人 DeepSeek 官方凭据使用 `profiles/private-cloud-workflow.json` 和 `DEEPSEEK_API_KEY`，端点为 `https://api.deepseek.com`。该部署的工具调用、auto/none 和 JSON 输出已用合成消息验证，记录见 `docs/experiments/20261010_web_retirement/official-capabilities.json`。两个显式云端 profile 均启用硅基流动 PDF OCR；PaperInput 入口不调用 PDF 解析与 OCR。

正式入口通过 `load_application_config` 解析、`preflight_config` 校验和 `prepare_startup` 保存启动快照。配置优先级为schema默认→配置分片→profile→支持的环境覆盖→显式override/CLI；对象合并，数组整体替换。实际值与字段来源、选用Prompt、源码内容、依赖声明及输入一起保存。

正式默认配置启用本轮五部分机制：Extractor 持久化阅读与生成／审核 Harness、Researcher
受控阅读与纯选项交互（`guided_reading=true`、`text_choices=true`、`research/choice_only`）、
Planner 有界修复与派发门控、Coordinator 按触发码独立补检，以及 Renderer 输入版本门控。
Researcher 的分轮预算随受控阅读启用；运行级预算保持原上限。
`load_config()` 的兼容配置转换也传递这两个开关。

`local-baseline`、`remote-baseline`、`local-reader-reuse`、`local-harness-guarded` 和
`local-harness-closure` 是历史实验 profile，显式保留原生工具循环。后两者的 checkpoint
与受控阅读不兼容；配置加载时拒绝同时开启。要在新实验中使用默认机制，应不选这些历史
profile，或显式设置两个开关、`research/choice_only` 提示词及 `enable_evidence_checkpoint=false`。

`profiles/local-harness-closure.json` 演示本轮闭环开关：保守查新点去重、Reader重用、Builder checkpoint、机械状态投影和精确context准入；单轮单并发，最多40模型调用、12次物理数据库请求，只启用arXiv，Provider自动回退列表为空。它是实验配置，不代表已验证小模型科研效果，也不代表获得新的外部检索授权。

只读检查，不调用模型或数据库：

```bash
PYTHONPATH=backend/src:. python scripts/inspect_config.py \
  --profile config/profiles/local-harness-closure.json \
  --isolated-config --entrypoint paper_input --output /tmp/novelty-config.json
```

示例本地模型读取 `LOCAL_VLLM_API_KEY`。对确实未启用鉴权的本地服务，可明确设置 `LOCAL_VLLM_API_KEY=local` 作为客户端占位；开启鉴权的服务应提供其真实密钥环境变量。端点默认 `http://127.0.0.1:8000/v1`。不要将占位值当成服务授权验证。PDF入口还需检查实际processing角色；PaperInput入口不调用PDF处理器。

主要新增开关：

| 路径 | 默认 | 作用 |
| --- | --- | --- |
| point_extractor.harness.enabled | true | 唯一提取路径；只接受 true，旧流程及 prompt_names/conservative_dedup 已移除 |
| researcher.harness.guided_reading | true | 按阶段读取、检索策略递减与分轮预算 |
| researcher.harness.text_choices | true | 动态选项契约，模型选择编号、程序执行工具 |
| researcher.harness.reuse_reader_results | false | 相同可信材料范围重用与EOF约束 |
| researcher.harness.enable_evidence_checkpoint | false | 暂存Builder验证通过的局部卡，最终Validator/Reviewer仍独立执行 |
| researcher.harness.runtime_state_projection | false | 每次调用显示当前任务、预算、读取、Provider和checkpoint事实；不压缩原文 |
| models.<alias>.context_admission.mode | off | observe/enforce；精确计数器不可用时按on_unavailable显式处理 |
| project.workflow.recovery_provider_order | [] | 仅在已启用且已预检的来源间按顺序恢复，不自动启用Provider |
| project.processing.ocr_fallback_enabled | false | 本地默认关闭云端OCR；显式云端profile启用 |

改变模型能力需要该部署的明确声明，未知tool_calling/json_object/vision不会自动推断为支持。Context计数器示例针对本地vLLM 0.8.5协议；其他服务需选择正确计数方式，不能仅改窗口数字。

`remote-baseline.json`、`local-baseline.json`、`local-reader-reuse.json` 和 `local-harness-guarded.json` 保留原实验条件。新实验使用新运行目录，不覆盖旧报告/配置/源码快照。配置冻结可以复现客户端条件，不保证远端模型或Provider随机结果一致。详细依据及恢复接口见 [本轮配置报告](../docs/experiments/20260927_harness_config_closure/configuration.md)、[恢复报告](../docs/experiments/20260927_harness_config_closure/recovery.md)和[Reviewer恢复说明](../docs/experiments/20260927_harness_config_closure/reviewer_recovery.md)。

arXiv 默认使用现有网页检索通道（`search_transport: "web"`），PaperInput/Web 启动时的参考文献初始化也读取同一份 Provider 配置。需要 API 时，显式设置 `researcher.tools.database_search.providers.arxiv.search_transport` 为 `"api"`。已有参考文献缓存继续复用。

### Researcher 的分轮预算

开启 `researcher.harness.guided_reading` 后，Harness 按 Planner 的原始策略依次执行最多三轮：保留全部策略、移除 strict、再移除 medium。每轮独立使用 `harness.per_tool_limits` 中各工具的调用额度。例如 `database_search: 8` 表示每轮最多 8 次调用，三轮合计最多 24 次，不再共用 8 次；Reader 等工具使用同样的分轮计数规则。没有有效策略的轮次不会创建，也不会领取额度。

摘要读取和全文获取计入文献所属的轮次。停止检索后集中阅读正文时，仍扣除各正文原轮次的 Reader 额度，不会把所有正文都算到第三轮。失败的工具执行也扣额度；同轮额度用尽后仍按既有机制结束探索并收尾，不能借用其他轮次的额度。

为避免任务总额度继续把三轮截成一轮，Harness 的模型轮次、总工具调用次数、总读取字符数上限按实际可执行轮数扩大。例如三轮时，基础配置的 28 个模型轮次、24 次工具调用、192000 个读取字符，对应任务上限为 84、72、576000。这些是任务级总上限；各工具仍另外受每轮次数约束。补检恢复指令只获得一轮额度；未启用 guided reading 的旧入口维持原任务级语义。运行级的模型调用、物理数据库请求、截止时间等硬限制保持有效，不会自动放大。

“篇数”与“工具调用次数”是不同预算。历史字段 `tools.database_search.candidate_limit_per_task` 实际一直限制每次检索返回的候选篇数：设为 8 时三轮最多可返回 24 篇候选，重复文献不会因此变成不同文献。一次 `database_search` 调用可以返回多篇候选，而后续获取全文也会使用该工具的调用次数；若要每轮检索一次并分别获取 8 篇全文，`database_search` 每轮调用额度至少应为 9。Provider 内部各检索式及回退共用本次检索额度，不额外创建 Harness 轮次。

调用轨迹的 `research_round:N` 标记和预算状态的 `remaining_per_tool_by_round` 记录每轮计数，便于核对实际消耗。
