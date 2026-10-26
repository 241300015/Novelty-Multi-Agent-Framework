# Issue #20 本地工作报告

日期：2026-10-18—2026-10-19

Issue：https://github.com/241300015/Novelty-Multi-Agent-Framework/issues/20

代码目录：`Novelty-Multi-Agent-Framework`

基线提交：`aae5cc542eb55bd2f581eb502b9e8e1924a75518`。Issue #20 实现提交为
`35b05de5291684096c8dd0d06316b8b26243819a`；未推送或关闭远端 issue。

## 完成内容

1. **两个入口统一落盘阅读索引。** PDF 解析后的 `persist_paper_input` 和直接输入 `persist_workflow_input` 共用快照登记；沿用 content-list.json 入口、原子 JSON 持久化和现有 runtime manifest，参考文献 manifest 保持原有职责。每次输入独立保存 PaperInput 与分段索引，旧运行的来源不受后续输入覆盖。
2. **最小索引契约。** 包含运行 ID、论文身份、产物类型、相对路径、版本、SHA-256、生成阶段和状态。运行 manifest 连接冻结索引引用。契约写入 `Novelty-Multi-Agent-Framework/docs/point-extractor-paper-reading.md`，供 P0-06 后续复用；不宣称已实现 P0-06 的报告编译器。
3. **提取器真正读取正文。** PaperDigest 保留导览，新增持久化引用；模型按分段索引选择正文，程序从文件重载并校验，读取结果进入生成、去重与补充请求。每段最多 4000 Unicode 字符，最多 24 次阅读决策，原有生成/审查预算保持 5 次。
4. **成果定位与错误轨迹。** 来源携带运行 ID、索引哈希和字符范围；禁止未读段和伪造来源进入成果。成功及失败轨迹同时写入论文工作目录、不可变输入快照目录，并按需写入 runtime diagnostics。包括提取器启动前加载失败、参数纠正、版本/哈希/缺文件异常与预算终止。
5. **职责与提示词。** 增加可配置 `prompt_names.read`；生成、去重、补核查仍各自使用现有提示词位置。文档明确数量不足、错误输出、完全同 claim 合并和语义改写重复的真实边界。未读段保留覆盖警告，不以数量达标宣称完整。
6. **修复旧正文问题。** 修复前实测：同 paper_id 第二次输入 new body 后 full.md 仍是 old body；修复后兼容视图同步新正文，旧输入仍可凭冻结快照读取。

## 验证证据

测试解释器：`/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/python`，使用既有依赖环境，未安装或修改依赖。

| 验证 | 结果 |
| --- | --- |
| 最终相关回归（提取、持久化、工作流、提示词/配置、运行产物、PDF 文本层、Renderer） | **156 passed**，5 条 PyMuPDF/SWIG 弃用警告 |
| 完整离线回归（最终补充“启动前加载失败”分支之前的检查点） | **1411 passed，5 failed，6 deselected** |
| 修改前基线重跑这 5 个失败测试 | **相同 5 项失败**，错误类型与原因一致 |
| git diff --check | 通过 |

日志：
- `issue-20-targeted-tests.log` / `issue-20-targeted-tests.xml`：最终相关回归。
- `issue-20-tests.log` / `issue-20-tests.xml`：完整离线回归。
- `issue-20-baseline-tests.log` / `issue-20-baseline-tests.xml`：基线复核。

基线复核从 Git HEAD 导出 backend/config/tests/scripts/pyproject.toml 到临时只读对照源码目录运行，不修改当前工作区。既有失败为：
- `test_api.py::test_only_current_task_api_is_started`：当前依赖中的 `_IncludedRouter` 没有 `path`。
- `test_reviewer_benchmark.py` 中 3 项：缺少 `backups/MF2033k6lC/v2/references/documents/...txt` 基准材料。
- `test_runtime_config_injection.py::test_perturbed_config_reaches_runtime_objects`：`ArxivWebSearchTool` 没有 `_min_interval`。

这 5 项不在本次 issue 范围内，未为追求全绿而改动、屏蔽或伪造数据。

## 可直接检查的长文证据

同目录 `issue-20-demo/summary.json` 与
`issue-20-demo/long-paper/point_extraction_trace.json` 保留一组可检查运行产物：
- 正文共 **13660** 字符；后段关键字 `LATE-OMEGA` 不在原摘要摘录中。
- 模型选择 `body-4`，程序实际读取 **[12000:13660]**，生成请求断言包含后段正文及精确来源。
- 3 条确定性测试成果均保存了该段的版本化来源；总调用 4 次。
- 未读 body-1～body-3 明确保留，`coverage_complete=false`。

`tests/test_point_paper_reading.py` 还验证：
- 真实生成 8 页 PDF → text_layer 解析 → PaperInput → 持久化；以及直接 PaperInput 持久化。
- 两个入口都启动独立 Python 子进程，只传持久化目录：逐段重构全文、运行提取器读取后段并生成带来源的成果。
- 文件缺失、哈希错误、运行/版本不符、路径越界、摘要身份混用、错误分段选择、有界修正、伪造引用、阅读预算耗尽、运行 manifest 关联。
- 关闭 runtime debug 时，成功和失败仍有独立的提取轨迹。

## 边界

- 离线回归使用确定性模型替身，证明持久化/真实阅读/上下文传递/定位链路；SSH live 使用真实模型验证核心链路，但不据此声称实际论文提取质量已经合格。
- 字符定位基于持久化 PaperInput.full_text，非 PDF 页码或 OCR 坐标；未改写 MinerU/OCR。
- 已读来源检查不等于 claim 的语义支持认证。补充后仅做完全同 claim 合并，没有新增第二轮语义去重，相关质量应由 P0-08-E 对照验证。
- 超过阅读预算或主动结束后仍有未读段时，保留范围限制；不宣称全文完整阅读。
- 复用集中 FailureCode 并在本节点 trace 留痕；未把全仓旧 Extractor/Planner 错误迁移到统一 FailureEvent，P0-02 仍独立推进。

## 复现最终相关回归

在 `Novelty-Multi-Agent-Framework` 目录运行：

```sh
/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/python -m pytest \
  tests/test_point_paper_reading.py tests/test_point_extractor.py \
  tests/test_point_coverage.py tests/test_workflow.py tests/test_persistence.py \
  tests/test_operation_prompt_config.py tests/test_configuration_startup.py \
  tests/test_runtime_artifacts.py tests/test_processing_text_layer.py tests/test_renderer.py
```

## Live 测试结果（2026-10-19）

新增的 `tests/test_live_point_paper_reading.py` 已使用真实 `qwen2.5-7b-instruct`
通过验收：`1 passed in 24.69s`。8330 字符合成长文的 `HELIOS-LATE` 贡献位于
`[8000:8330]`，不在摘要摘录中；模型实际读取全部三个分段，最终候选全部引用版本化
后段位置。7 次真实调用共消耗 18,194 输入 token、874 输出 token、19,068 总 token，
无失败调用。模型未配置价格，成本仍为未知，不能记为免费。

Live 同时确认保守去重边界：模型认为第 2、3 条是第 1 条的语义改写，但字面覆盖守卫
无法证明 claim 等价，因而保留三条并记录 `pending_indices=[2,3]`；
`coverage_complete=false`。这次结果证明全文读取和定位链路可用，不证明最终语义去重或
真实论文提取质量已合格。完整记录见同级 [live-validation.md](live-validation.md)。

在 SSH tunnel 已建立且本地环境变量已加载时，可用以下命令复现：

```sh
ISSUE20_LIVE_ARTIFACT_DIR=docs/experiments/20261019_issue20_paper_reading/live \
/Users/Admin/Documents/projects/rwzp-novelty/9.14/.venv/bin/python \
  -m pytest -m live tests/test_live_point_paper_reading.py -s -vv
```

本阶段所有真实模型请求均通过当前 SSH tunnel 访问 vLLM 服务。摘要兼容入口不作为本次
验收依据；此次没有执行 PDF/OCR、检索、Reviewer 或全图工作流。

## 验收结论

按 Issue #20 的明文验收标准，本次实现已经满足要求，可以验收。核心链路同时具备代码、
离线测试和 SSH live 测试证据；当前没有阻塞 Issue #20 验收的缺口。

| Issue 验收项 | 结论 | 证据 |
| --- | --- | --- |
| 长文中位于原摘要截断范围外的贡献可被读取并定位 | 通过 | 离线样本的 `LATE-OMEGA` 位于 `[12000:13660]`；live 样本的 `HELIOS-LATE` 位于 `[8000:8330]`，均不在 PaperDigest 摘录中，最终成果保存版本化字符范围 |
| PDF 解析后的 PaperInput 和直接 PaperInput 都能在新进程仅凭持久化产物恢复全文 | 通过 | `tests/test_point_paper_reading.py::test_fresh_process_reads_entire_input` 参数化覆盖 `parsed_pdf` 与 `direct`，子进程只接收论文持久化目录 |
| 分段读取和成果引用可检查 | 通过 | 阅读 trace 保存实际 segment、字符范围、索引哈希、模型可见正文和最终 `source_locations`；伪造或未读来源不能进入成果 |
| 长文、缺文件、版本混用和失败路径可检查 | 通过 | 测试覆盖缺文件、正文哈希错误、run/version 不符、路径越界、Digest 身份混用、错误选择纠正、阅读预算耗尽及节点启动前失败 |
| 最小运行产物索引可关联到运行 | 通过 | 索引包含 run ID、paper ID、产物类型、相对路径、版本、SHA-256、生成阶段和状态，并由 runtime manifest 连接冻结引用 |
| 生成、去重、补充和错误处理边界明确，数量不等于完整性 | 通过 | 文档和 trace 分别记录阅读覆盖、候选生成、删除建议、保守守卫、补充核查、待定重复及失败；达到 3 条不会把 `coverage_complete` 自动设为 true |
| 无需重写 OCR | 通过 | 字符定位基于持久化 `PaperInput.full_text`；未修改 MinerU/OCR，PDF 页码和 OCR 坐标也不被伪装为当前字符定位 |

## 验收后的已知缺口

以下事项不阻塞 Issue #20 的明文验收，但应作为后续质量和跨 issue 集成工作保留：

1. **去重和覆盖状态尚未独立上报。** Live 中模型建议删除第 2、3 条候选，但保守守卫无法证明 claim 等价，留下 `pending_indices=[2,3]`。随后覆盖账本将最终 `scope_status` 设为 `pending_coverage`，工作流只能提升其中一种警告。详细去重事实仍保存在 `deduplication.coverage_issues`；后续宜拆分 `dedup_status` 与 `coverage_status`，或允许并列 pending 状态。
2. **真实论文质量尚未进行 live 对照。** 当前 live 使用合成长文验证真实模型的读取和定位；真实生成 PDF 的解析、持久化与跨进程恢复由离线测试覆盖，但尚未通过完整工作流对实际论文执行提取质量对照。该项属于 P0-08-E 一类质量验证。
3. **语义重复仍需可靠验证。** Live 的 3 条候选实质上是同一贡献的不同改写。程序为避免无依据误删而正确保留并标记待定，但最终候选不能据此解释为 3 个已经确认相互独立的查新点。
4. **P0-02/P0-06 的全局能力不在本 issue 内完成。** 本实现复用集中 FailureCode，并提供可供 P0-06 消费的最小索引契约；尚未把全仓旧 Extractor/Planner 错误迁移到统一 FailureEvent，也未实现 P0-06 报告编译器。
5. **单次运行存在阅读预算。** 当前最多执行 24 次阅读决策、每段最多 4000 Unicode 字符。超过约 96000 字符时仍可通过索引选择后段，但不能保证一次运行读取所有分段；系统会保留未读列表并将覆盖标记为未完成。
6. **来源校验不等于语义支持认证。** 当前校验可以证明模型读过所引用的版本化正文范围，不能仅凭字符范围证明该正文在语义上充分支持 claim；该判断仍需后续质量评估。

因此，Issue #20 可按当前验收标准关闭；以上项目应作为后续任务记录，不应被解读为本次全文读取与定位链路未通过。
