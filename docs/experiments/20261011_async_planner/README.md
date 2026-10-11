# SearchPlanner 异步并发验证（2026-10-11）

工作流的 Planner 节点原先在异步函数内直接调用同步 `plan()`，模型等待期间阻塞事件循环。本次新增 `SearchPlanner.aplan()`，工作流优先调用该接口，真实 Planner 使用 `await ModelClient.acomplete()`；同步脚本仍可在事件循环外调用 `plan()`。两种入口共享提示词构造、重试、草稿校验及编译流程。Demo 支持异步接口，旧同步实现通过线程兼容。

模型客户端沿用已有 `acomplete()` 实现，其底层同步传输在线程中执行；本次没有替换 HTTP 传输层。取消协程会停止 Planner 重试，但已派发的底层请求可能继续完成，由现有客户端记录迟到结果。

## 本地真实模型验证

- 模型：`qwen2.5-7b-instruct`；地址：`http://127.0.0.1:8000/v1`。
- 输入：现有 MF2033k6lC 论文与前两个已保存的查新点。
- 使用生产 Coordinator、规划分发、Planner 节点及研究分发完整性门控构建规划子图；并发上限为 2。
- 两条任务均生成合法 SearchPlan，通过关联检查和完整性门控，并保存检索规划。
- 总耗时 17.267 秒；两个首次模型调用重叠 9.323 秒；事件循环心跳计数 846。
- 每条任务实际调用模型两次。首次分别因概念超限、排除词冲突被编译器拒绝；错误反馈后的第二次均通过。
- Planner 事件正确关联各自的 stage、point/task 和模型调用，没有跨任务混淆。

证据见 `live-summary.json`（调用时间、阶段绑定、编译反馈和最终方案）及 `retrieval-plans.json`。运行完整记录保存在 `/tmp/novelty-async-planner-vllm-20261011`。此验证覆盖规划阶段，不执行文献检索和报告生成。

另在本机 Ollama `qwen2.5-coder:7b` 上完成两任务验证，耗时 47.726 秒，两个合法方案，规划任务峰值为 2。

复现命令（仓库根目录，已连接本地服务）：

```bash
PYTHONPATH=backend/src:. /home/lya3106643285/miniconda3/envs/Novelty/bin/python \
  scripts/probe_planner_async.py --output /tmp/novelty-async-planner-probe
```

探针仅允许本地端点，读取 `LOCAL_VLLM_API_KEY`（未设置则使用占位值），不读取或修改 `.env`。有代理的环境需要将本地地址加入 `NO_PROXY`。

## 回归与错误行为

新增验证覆盖模型等待重叠、独立消息上下文、并发上限及汇合屏障、异步重试反馈、重试耗尽、取消传播、同步入口误用检查，以及异步模型调用的诊断关联。相关回归 120 项通过。

规划失败仍采用现有规则：记录单任务失败，但汇合时完整性门控要求所有任务具有方案，因而停止运行。本次没有改变这个规则。失败记录和门控行为都有已有回归覆盖。

全量回归在 Novelty 环境运行，设置 `NOVELTY_EXPERIMENT_PROFILE=` 排除当前 `.env` 的云端配置对默认配置测试的干扰。结果和 main 对照见 `regression-summary.json`。三个 Reviewer 基准失败依赖此前已删除的数据，另一个失败是旧 arXiv 搜索接口断言。
