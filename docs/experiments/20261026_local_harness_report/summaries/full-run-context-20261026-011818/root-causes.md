# 本次运行的代码层问题定位

仅调查和离线复现，未在本次调查中修改生产代码，也未重新调用模型或文献 API。

## 1. 提示词解析在空行处截断（确定的程序错误）

`backend/env/prompt_library.py:57` 遍历 front matter 时，只把以空格或 tab 开头的行视为块内容。空行不满足此条件，于 62–65 行提前写入并结束 system 块；后面的缩进规则因此丢失。

离线最小复现：system 块包含 first、空行、second，解析结果仅为 first。实际 SearchPlanner 模板解析出的 system 只有 214 字符，概念规则、英文检索要求及正例全部未包含。运行第 4、13 次调用的请求也证实，只发送了角色介绍和后追加的中文解释约定。后追加约定仍要求检索词遵守任务语言，因此不能把问题解释为模型完全没有收到任何语言要求。

## 2. 检索语言校验过弱（确定的检查缺口）

`agents/search_plan_compiler.py:30,200` 用 `[A-Za-z]` 是否出现判定 en 任务是否具有英文词项。GSAERU模型、GNN模型这样的混合词即可满足检查。

实际输入包含 language=en 和英文 claim_en，但模型返回中文检索词，校验放行；最终生成 ti:"GSAERU模型" 等查询。第一任务 10 条、第二任务 6 条查询均零命中。不能据此证明相关文献不存在，也不能仅凭这一轮量化各因素对零命中的贡献。

## 3. Coordinator 补检任务类型不符合契约（确定的模型输出错误）

第 10 次模型调用针对 evidence.missing 返回 action=research_gap，却在 research_tasks 中写 task_type=literature_search。

`core/coordinator_policy.py:37` 定义该触发器的任务类型为 research_gap；`core/recovery_planning.py:92` 校验拒绝，生成 coordinator.task.type_invalid。校验本身符合现有契约。另一个 coverage.no_match 分支生成 replan_query，得以继续。

## 4. 补检菜单与预算轮次矛盾（确定的程序错误）

`workflows/research_reading_policy.py:38` 为 replan_query 构造三个检索阶段；第 55 行只对 change_provider/retry_request 收缩阶段，但第 89 行对所有 recovery 一律只提供一个预算范围。

第一个检索阶段结束后 round_index 变为 1，菜单继续提供“第 2 轮检索”，budget_scope 返回 2，而 budget_scope_count 仍为 1。`core/tool_call_harness.py:555` 的合法性检查因此抛出 ValueError。实际第 15 次模型调用选择的 v2_0 是程序发放的合法选项，不能把此错误归咎于模型乱选。

使用本次冻结的 SearchPlan 和模拟零命中 observation 已离线复现：rounds=3、allowed_scope_count=1、next_scope=2。没有发出网络请求。

## 5. 通用异常分支丢失任务内已有结果（确定的程序错误）

`workflows/research_task.py:179` 捕获普通 Exception 后返回 partial 和空 trace。上述预算检查抛出的是普通 ValueError，没有进入能保留 trace 的 ToolCallHarnessError 分支。之后 search_executions 从空 trace 构造，因此任务结果显示查询数 0、steps_used=0。

原始 retrieval_events 明确保存第二任务 6 条已成功执行的查询；第 15 次模型请求也带有 succeeded=6 的工具观察。故需区分“没有执行”与“执行过但未回填”。此前审计已更正，原始产物未改写。

## 6. 报告自由文本越过证据边界（确定的输出问题）

第 16 次模型调用在证据卡为 0 时写出“已有文献未涉及此技术细节”，报告保留了这句话。结构化状态仍为 insufficient_evidence、verdict=null，但这不能为自由文本中的断言提供依据。

此外，Extractor 仅补读正文 4,000 字符，留下 20 个未读分段，coverage_complete=false。完整流程结束不代表全文贡献覆盖已完成。上述问题都不能归因于上下文溢出：16 次请求的精确计数与服务端一致，峰值输入加预留输出只有 13,222 / 32,768 tokens。

建议先修复提示词解析和预算范围矛盾，再修复异常结果保留，并收紧检索语言与报告断言校验；修复后用原输入重新验证。
