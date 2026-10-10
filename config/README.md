# 可复现实验配置

当前服务使用 `profiles/cloud-workflow.json`：五个查新角色和标题模型均调用硅基流动 DeepSeek Flash，工作流并发上限为 4。启动前设置 `NOVELTY_EXPERIMENT_PROFILE=config/profiles/cloud-workflow.json` 和 `SILICONFLOW_API_KEY`（见 `backend/.env.example`）。角色环境变量仍可覆盖 profile。该配置不改变现有论文、证据和报告持久化机制。

正式入口通过 `load_application_config` 解析、`preflight_config` 校验和 `prepare_startup` 保存启动快照。配置优先级为schema默认→配置分片→profile→支持的环境覆盖→显式override/CLI；对象合并，数组整体替换。实际值与字段来源、选用Prompt、源码内容、依赖声明及输入一起保存。

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
| point_extractor.conservative_dedup | true | 缺乏机械覆盖证明的跨claim删除保留待核查 |
| researcher.harness.reuse_reader_results | false | 相同可信材料范围重用与EOF约束 |
| researcher.harness.enable_evidence_checkpoint | false | 暂存Builder验证通过的局部卡，最终Validator/Reviewer仍独立执行 |
| researcher.harness.runtime_state_projection | false | 每次调用显示当前任务、预算、读取、Provider和checkpoint事实；不压缩原文 |
| models.<alias>.context_admission.mode | off | observe/enforce；精确计数器不可用时按on_unavailable显式处理 |
| project.workflow.recovery_provider_order | [] | 仅在已启用且已预检的来源间按顺序恢复，不自动启用Provider |
| project.processing.ocr_fallback_enabled | true | false同时禁用OCR构造和对应预检 |

改变模型能力需要该部署的明确声明，未知tool_calling/json_object/vision不会自动推断为支持。Context计数器示例针对本地vLLM 0.8.5协议；其他服务需选择正确计数方式，不能仅改窗口数字。

`remote-baseline.json`、`local-baseline.json`、`local-reader-reuse.json` 和 `local-harness-guarded.json` 保留原实验条件。新实验使用新运行目录，不覆盖旧报告/配置/源码快照。配置冻结可以复现客户端条件，不保证远端模型或Provider随机结果一致。详细依据及恢复接口见 [本轮配置报告](../docs/experiments/20260927_harness_config_closure/configuration.md)、[恢复报告](../docs/experiments/20260927_harness_config_closure/recovery.md)和[Reviewer恢复说明](../docs/experiments/20260927_harness_config_closure/reviewer_recovery.md)。

arXiv 默认使用现有网页检索通道（`search_transport: "web"`），PaperInput/Web 启动时的参考文献初始化也读取同一份 Provider 配置。需要 API 时，显式设置 `researcher.tools.database_search.providers.arxiv.search_transport` 为 `"api"`。已有参考文献缓存继续复用。
