# Issue #21 工作报告

## 结论

已在本地 `./tt` 完成 Issue #21 的统一错误码目录、节点本地纠错审计契约、运行时历史／索引和查询入口，并在 `ResearcherToolRegistry` 与 `ToolCallHarness` 接入四类可重复验证的代表性路径。这些是有效的第一阶段交付，但不等于 Issue 全部验收已通过。

本实现没有建立集中式纠错调度器，也不把“发现错误”或“建议动作”伪装成已执行。Reader、Research、Builder、Validator、Reviewer、Harness 仍在原责任位置拥有纠错权；Gate A 只负责发现并阻断非法输入，错误的处理权元数据指回 Reviewer。

> **2026-10-19 验收复核结论：当前不满足 Issue #21 全部验收标准，不建议关闭 Issue。** GitHub 上该 Issue 仍为 OPEN，当前实现可评为“契约与代表性链路通过，逐节点原地纠错和强预算／版本约束未闭环”。下文“当前 Issue 验收审计”取代先前仅根据代表性测试得出的乐观结论。

## 交付内容

### 1. 集中错误目录

- `schemas/failures.py` 中 `FAILURE_CATALOG` 与 `FAILURE_DESCRIPTIONS` 均完整覆盖 148 个 `FailureCode`：27 个原分类码和 121 个具体原因码。
- 保留 13 个 `RecoveryAction`，不新增隐式动作。
- 每个错误码现在都有：可核实失败事实、触发条件、layer/category、拥有处理权的节点与函数、允许动作、独立重试上限、停止边界、实现状态和验证状态。
- `gate_a.*` 明确区分发现位置 `NoveltyWorkflow._validate_synthesis_input` 与处理权 `NoveltyEvidenceReviewer._review_point`。
- `docs/error-and-supplement-codes.md` 是唯一人可读集中目录；具体码表与最长前缀责任表可以确定性联接，程序目录则已为每个码展开全部元数据。

状态中的 `implemented / automated` 只表示失败事实的检测／断言已有代码和自动用例，不表示对应业务恢复已全部自动化；粗分类和跨节点闭环继续以 `partial / integration_only` 标识。`model.usage_unavailable` 明确保留为 `not_implemented / not_verified`。

### 2. 纠错生命周期与记录契约

新增独立 `CorrectionRecord`，状态只能沿以下方向推进：

```text
detected
  ├─ advised → executing → verification_pending → verification_passed
  │                         └→ attempt_failed → advised / executing / stopped
  ├─ executing → ...
  └─ stopped
```

关键约束：

- `verification_passed` 必须有复核器、复核时间和通过状态，且不能残留未解决项。
- `attempt_failed` 必须有失败复核和剩余问题；`stopped` 必须保存实际终止动作与原因。
- 纠错记录关联 run、paper、point、task、source、SourceRecord、material、Card、Evidence，并可保存纠错前后 artifact namespace、ID、版本和 SHA-256。
- 每次执行保存 action input、局部／全局预算、尝试次数、结果、重新验证、剩余问题和停止原因。
- 本地纠错的补检轮次前后必须相同；同一链的全局预算上限不可改变、已用预算不可减少，尝试次数不可回退。
- 各错误仍采用 `FailureDefinition.retry_limit` 和所属节点策略，不存在统一的“一律重试 N 次”。

### 3. 历史、索引与查询

`RuntimeArtifactManager` 为每个 run 新增：

- `corrections/events/*.json`：不可覆盖的每次状态快照；
- `corrections/index.json`：每条链的最新状态、所有事件引用、状态计数和未解决 ID；
- `manifest.json.correction_index`：索引稳定入口；
- `summary.json.corrections`：计数、未解决 ID 和事件数。

Web 服务新增：

- `RunSnapshot.progress.corrections`：运行进度中的计数、未解决 ID 和事件数；
- `RunReport.corrections_url`；
- `GET /api/novelty/runs/{task_id}/corrections`：读取该任务最新 run 的完整纠错索引。

审计索引没有写进业务 `NoveltyReport`，因此打开／关闭运行时追踪不会改变业务结果。

### 4. 已接入的实际路径

`ResearcherToolRegistry`：

- 参数或作用域拒绝时生成 Failure 与 `detected` 记录；
- 有本地修正建议时记录 `advised`，下一次同 scope/tool 的合法调用记录真实 `executing`；
- 调用完成后记录 `verification_pending` 及复核通过或失败；
- 修正后工具仍失败时保留 `attempt_failed` 并明确 `stopped`；
- 未注册工具等无自动恢复路径直接以实际 `manual/stop/none` 动作结束，尝试次数保持 0。

`ToolCallHarness`：

- 总工具次数、单工具次数、读取字符和探索轮次耗尽均记录对应具体错误码；
- 预算拒绝直接进入 `stopped`，不会伪造一次重试，也不会增加任务级补检轮次。

## 当前 Issue 验收审计（2026-10-19）

复核以 [GitHub Issue #21](https://github.com/241300015/Novelty-Multi-Agent-Framework/issues/21) 当前正文为准（状态 `OPEN`，更新时间 2026-10-17T16:04:54Z）。“部分满足”表示契约或代表性样例存在，但尚不能支撑 Issue 要求的逐码、逐层或真实产物结论。

### 实施条款对照

| Issue 实施条款 | 判定 | 当前证据 | 缺口 |
| --- | --- | --- | --- |
| 唯一集中目录，同步 148 个错误码和 13 个动作 | **部分满足** | 枚举、`FAILURE_CATALOG`、描述和文档数量均为 148/13；自动测试要求每个码／动作字符出现在文档中 | 同步测试只验证“字符出现”，没有解析文档并逐字段比对事实、owner、actions、停止边界和状态；文档与程序仍可静默漂移 |
| 每码补足事实、触发、节点／层级、owner 函数、actions、停止边界、实现／验证状态 | **部分满足** | 148 个 `FailureDefinition` 的对应字段非空；状态统计为 121 `implemented/automated`、26 `partial/integration_only`、1 `not_implemented/not_verified` | 121 个具体码的状态由“是否在 `FAILURE_FAMILIES`中”统一推导，不是逐码证据审计；至少 44 个目录的 owner 仍是 `provider ... adapter`、`emitting boundary`或 `A / owning Agent parser` 类泛化文本，无法唯一定位真正 callable |
| Reader/Research/Builder/Validator/Reviewer/Harness 保留原地纠错，跨节点拒绝回传 owner | **未满足** | `correction_trace` 的生产调用只出现在 `ResearcherToolRegistry` 和 `ToolCallHarness` | Reader、Builder、Validator、Reviewer 未接入 `CorrectionRecord`；Gate A 只有 owner 元数据，没有回传 Reviewer 并在原位置执行／复核的链路；集中文档也明确将其留给 P0-03/04 |
| 每次纠错记录 scope、对象、码、输入、预算、尝试、结果、复核、剩余问题／停止原因及产物版本 | **部分满足** | `CorrectionRecord` 具备全部字段，Runtime 保留不可覆盖快照；人工构造的契约测试验证了 before/after hash | 实际 Registry/Harness 路径没有填充产物版本；global budget、supplement round 及大部分 Card/Evidence/material 句柄也可为空；尚无一条产品路径证明真实 Card/Evidence 修正前后的版本关联 |
| 区分发现、建议、执行、复核通过、尝试失败、不可继续，支持进度／报告查询 | **基本满足** | 状态机、events/index/manifest/summary、进度投影和 corrections API 均有离线及 live 验证；仅建议不能进入 `verification_passed` | `get_corrections()` 通过 mtime 从任务目录中选“最新索引”，未与 RunSnapshot/报告的确切 runtime run ID 绑定；多 paper/run 时可能查到错误链 |

### 验收条款对照

| Issue 验收条款 | 判定 | 原因 |
| --- | --- | --- |
| 文档目录与程序一致；典型错误可追踪到真正负责的本地代码 | **部分满足** | 数量和字段存在性一致，但缺逐字段同步验证，且数十个 owner 仍是泛化路径；`implemented/automated` 状态不是逐码实证 |
| 逐层覆盖可修复、修复失败、不可恢复、预算耗尽；有限重试不重置全局预算、不耗费任务级补检轮次 | **未满足** | 四类样例只覆盖 Registry/Harness，不是 Reader/Research/Builder/Validator/Reviewer/Harness 逐层；当前记录器允许将已有 global/supplement 字段替换为全 `None` 的预算快照，且 `retry_limit=1` 的错误可接受 `attempt=99`，因此“有界且不重置”只验证了部分输入形式 |
| 可查询完整纠错链，证据及版本关联正确，未支持项有明确终态，不统一承诺重试次数 | **部分满足** | 单链事件历史、状态计数和 API 查询已通过，目录也有 0/1 两类建议上限；但真实节点没有产物版本链，未接入节点根本不会产生明确终态，而且执行层未校验 attempt 不超过逐码上限 |

### 已确认通过的代表性能力（不等于整体验收）

| 能力 | 结果 | 证据 |
| --- | --- | --- |
| Registry 参数修正后执行并复核成功 | 通过 | 完整 5 状态事件链 |
| Registry 修正执行后仍失败 | 通过 | `attempt_failed → stopped`，保留剩余问题 |
| 未注册工具无自动恢复路径 | 通过 | `manual`、attempt=0、明确停止原因 |
| Harness 预算耗尽 | 通过 | 具体预算码、`stopped`、attempt=0 |
| 单条 Runtime/FastAPI 纠错链查询 | 通过 | live 用例验证 5 事件、scope、action input 和 no-store |

### 关闭 Issue 前的最小必要工作

1. **逐节点接线**：在 Reader、Builder、Validator、Reviewer 以及完整 Research 边界的原 owner 函数写入 `CorrectionRecord`；Gate A 拒绝必须实际返回 Reviewer，并记录执行与复核。
2. **强化预算与尝试不变式**：一旦链上出现 global budget/supplement round，后续快照不得将它们省略为 `None`；`attempt` 必须受错误码 `retry_limit`、节点局部上限和全局预算共同约束。
3. **真实产物血缘**：对实际 material/Card/Evidence 纠错填充 targets 与 before/after `ArtifactVersionRef`，验证历史有效成果不被覆盖。
4. **逐码审计可验证化**：用机器可读的单一源生成文档或反向解析文档，对 148 个码逐字段比对；`implemented/automated` 必须关联真实 emitter、owner callable 和测试 ID，不能按叶子码统一赋值。
5. **唯一 run 绑定**：在 RunSnapshot/报告保存确切 runtime run ID 或 index path，API 不再依赖 mtime 猜测要返回的索引。
6. **逐层验收用例**：每个责任层至少覆盖可修复、尝试失败／明确停止和预算边界；增加多 run 查询与进程重启后链路继续测试。

### 复核反例与计数

- 目录实测计数：148 个错误码、13 个动作；121 个 `implemented/automated`、26 个 `partial/integration_only`、1 个 `not_implemented/not_verified`；重试建议上限为 109 个 `1`、39 个 `0`。
- 代码搜索显示，除测试外的 `record_detection/record_execution/record_verification/record_stop` 生产调用仅在 `tools/researcher_registry.py` 和 `core/tool_call_harness.py`。
- 对 `tool.arguments.schema_invalid` 构造 `retry_limit=1` 的真实 `RuntimeArtifactManager` 链，将 `attempt` 直接设为 `99` 仍被接受并落盘。
- 同一链先记录 `global_limit=10/global_used=4/supplement_round=1`，后续传入全空 `CorrectionBudgetSnapshot()` 后，新快照的这些字段全部变为 `None` 且被记录器接受。这证明当前校验只阻止“两个快照均填值时”的回退，不阻止通过省略字段重置记录。

## 测试记录

运行环境使用临时依赖目录 `/tmp/novelty-issue21-deps`，未修改项目依赖或系统环境。首轮为非 live 回归；2026-10-19 补充了真实应用边界及无凭据 arXiv 网络冒烟，但未调用付费模型。

1. Issue #21 与相邻恢复／Runtime／Workflow 目标回归：**238 passed，0 failed**。
   - 证据：`issue-21-targeted.xml`
2. 仓库全量：**1432 tests，1418 passed，6 skipped，8 failed**。
   - 证据：`issue-21-full.xml`
3. 精确排除下述 8 条无关既有／环境失败后：**1424 tests，1418 passed，6 skipped，0 failed**。
   - 证据：`issue-21-regression.xml`
4. `git diff --check` 与相关 Python 文件 `py_compile` 均通过。

### Live 补充验证（2026-10-19）

| 测试 | 结果 | 实际边界 | 证据 |
| --- | --- | --- | --- |
| Issue #21 Runtime/FastAPI live | **1 passed** | 真实 Runtime 事件文件、index、FastAPI middleware 与路由；非手工写入 mock index | `issue-21-live-local.xml` |
| arXiv live smoke | **1 passed** | 真实 arXiv 搜索、元数据 resolve 与全文获取 | `issue-21-live-arxiv.xml` |

Runtime/FastAPI live 用例验证了：

- `detected → advised → executing → verification_pending → verification_passed` 五个真实落盘事件；
- `GET /api/novelty/runs/{task_id}` 的 correction 计数、未解决 ID 与事件数投影；
- `GET /api/novelty/runs/{task_id}/corrections` 的 run/paper/task scope、修正后 action input 及完整事件顺序；
- 两个 API 均带 `Cache-Control: no-store`。

arXiv live 冒烟返回文献 `1911.08795`，元数据解析成功，全文入口返回 20,598 字符。此测试无需 API Key，未运行 SiliconFlow、OCR、Point Extractor 或完整模型链。当前 shell 未设置 `SILICONFLOW_API_KEY`/`LOCAL_VLLM_API_KEY`；仅确认 `backend/.env` 存在，没有读取、展开或写入其内容。

两项 live 测试本身没有发现新回归，因此 live 补测阶段没有修改生产逻辑；这不代表 Issue 整体验收通过。本轮新增了可重复执行的 `tests/test_live_correction_trace.py`，并把 pytest `live` marker 说明从“仅真实 API Key/网络”修正为“真实应用／运行时边界或外部服务”。

全量的 8 条失败均不在本 issue 的业务修改路径：

- 1 条 `test_api`：测试遍历到没有 `path` 属性的 `_IncludedRouter`；
- 1 条 startup snapshot：隔离子进程清除临时 `PYTHONPATH` 后无法使用临时安装依赖；
- 2 条 Issue #20 paper-reading fresh-process：子进程只注入仓库路径，同样无法访问临时依赖；
- 3 条 reviewer benchmark：仓库缺少 `backups/.../art_*.txt` 基准语料；
- 1 条 runtime config injection：既有 `ArxivWebSearchTool` 没有测试所需 `_min_interval` 属性。

全量回归期间还观察到一次既有 Retry-After 时间精度抖动（7.0 秒得到 6.9999927 秒，越过容差约 0.0000003 秒）；该用例单独复跑及最终精确回归均通过，未加入排除清单。

## 修改范围

Issue #21 的主要文件：

- `backend/src/novelty_agent_framework/schemas/failures.py`
- `backend/src/novelty_agent_framework/core/correction_trace.py`
- `backend/src/novelty_agent_framework/core/runtime_artifacts.py`
- `backend/src/novelty_agent_framework/core/tool_call_harness.py`
- `backend/src/novelty_agent_framework/tools/researcher_registry.py`
- `backend/src/novelty_agent_framework/services/jobs.py`
- `backend/src/novelty_agent_framework/services/workflow_service.py`
- `backend/src/novelty_agent_framework/routers/runs.py`
- `docs/error-and-supplement-codes.md`
- `tests/test_correction_trace.py`
- `tests/test_live_correction_trace.py`
- `pyproject.toml`
- `tests/conftest.py`

`./tt` 在本 issue 开始前已有 Issue #20 的改动；本次没有回退、覆盖或清理这些改动。五项审查补修已形成一个本地提交，但没有推送或关闭远端 Issue。

## 明确保留的后续边界

2026-10-21 审查补修：失败 Observation 无 pending 也落审计；连续非法参数推进同一链并在目录上限停止；预算不可被空快照擦除；attempt 同时受目录与局部上限约束。新增 `tests/test_review_fixes.py` 和 corrections API live 用例。该补修不改变下列尚未完成的业务闭环范围，详细记录见工作区 `report/审查发现5项问题修复/工作报告.md`。

- Reader、Builder、Validator、Reviewer 的其余本地纠错点尚未逐一写入 `CorrectionRecord`；应由 P0-03/P0-04 在原所属函数接线。
- Gate A 到 Reviewer 的实际跨节点回传执行仍属于 P0-04；本 issue 已完成处理权元数据、非法输入阻断和审计契约，不伪造回传已执行。
- Provider 自动换源、真实语义补检质量和所有 148 个码的生产恢复闭环不属于本 issue 的完成声明。
