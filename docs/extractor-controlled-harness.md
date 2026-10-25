# Extractor 受控生成、语义审核与纠错

正式工作流与直接构造的 Extractor 均只使用 Harness，默认模型为本地 Qwen。
`harness.enabled` 仅接受 `true`；`false` 会报错，不再切换旧流程。
配置入口为 `backend/src/novelty_agent_framework/config/agents/point_extractor.example.json`；
可通过应用配置覆盖预算和提示词。模型参数默认 temperature=0、max_tokens=4096。

## 职责与入口

`NoveltyPointExtractorAgent.extract()` 统一交给
`core/extractor_harness.py`。必须提供被冻结索引绑定的持久化 PaperInput；不调用 PDF、MinerU 或 OCR，
不以调用方内存正文替代快照。正式工作流仍负责持久化输入、成果及成功／失败轨迹。

校验通过的完整 `PaperDigest` 以 `digest` 字段传入每次模型请求，包括首次阅读选择、生成、
补读选择、审核、修复、去重和格式纠错。Harness 不删除字段或再次裁剪正文节选；
digest 的内容和大小由构造逻辑负责。完整分段目录以 `content_list` 字段传入每次请求，包含 segment_id、start、end 和 preview，
不随阅读预算过滤；`options` / `reader_options` 才是当前预算内允许执行的动作。实际原文由 Reader 按需返回。
digest 导览不计作全文分段已读，也不扩展可引用的来源位置集合。

生成和语义审核使用不同系统提示词。生成负责首次提出候选和执行获准修改；审核负责原文支持、
贡献边界、语义重复、混合机制、引用及已读材料中的明确遗漏。审核不能直接改写候选。
Reader 是确定性的工具；阅读选择提示词只是选项入口，没有文件路径或自由偏移参数。

| 用途 | 配置字段（位于 point_extractor.harness） | 模板 |
| --- | --- | --- |
| 生成／按反馈修改 | generate_prompt | prompts/extractor/generate.md |
| 语义审核 | review_prompt | prompts/extractor/semantic_review.md |
| 首次阅读／少于三点时的补读选择 | reader_prompt | prompts/extractor/reader_choices.md |
| 局部纠错程序 | correction_prompt | prompts/extractor/correct.md |

四个模板都归属 Extractor，并纳入启动提示词摘要与版本记录。旧 `extractor/read_paper.md`、
`extractor/extract_points.md`、`reviewer/review_points.md` 已移除，不再保留第二套生成／去重流程。

## 执行与限制

1. Harness 校验 PaperInput 索引、身份和哈希，发放绑定本次输入的阅读选项。
2. 生成前显式询问是否需要阅读全文或补读分段。每次只选一个选项；预算允许时有 `read_all`，
   否则选择一个具体分段。读取后提供真实正文和版本化引用。
3. 首次生成最多 8 个候选，建议覆盖 3 个有依据的独立贡献，但没有硬性最低数量。编号由程序分配，模型只能引用已读位置。
   首次候选少于 3 个时，在语义审核前进入一次 `post_generation_reading`：模型可选择一个获准未读分段、预算内的 `read_all`，
   或 `continue_review` 并说明无需补读的依据。补读结果送入原审核／修复流程，由审核确认遗漏后授权补充；
   此阶段不直接改写候选，也不因修复后仍不足三点而循环触发。已无阅读选择额度或无预算内可读分段时，记录跳过原因并继续审核。
4. 审核须覆盖全部当前候选，返回通过项、不合格项及原因、原文依据。需要补读时不能同时裁定；
   审核只能选择程序发放的未读选项。省略可选 `read_requests` 表示没有工具请求，不代表补全阅读。
   对每个候选对还必须显式判断 `duplicate/distinct/uncertain`，并与逐点审核结论一致；
   不确定时不能同时通过两点。程序只验证判断的完整性与一致性，不替代语义判断。
5. 有重复关系时，先进入 `deduplicate`：Harness 取审核明确判重的一对候选，发放两个选项，
   每个选项只对应一个待保留的候选点，包含 `option_id`、`point_id` 和该点的内容 `point`。
   模型比较这两个点的解释质量，只返回 `{"option_id": "所保留候选对应的选项 ID"}`。
   Harness 保留所选点、删除另一个，按原顺序组装完整列表，其他候选不受影响。
   例如 `[1,2,3,4]` 只有 2、3 重复，LLM 只选择保留 2 或 3，程序返回 `[1,2,4]` 或 `[1,3,4]`。
   每次去重使列表减少一个元素，随后复审整个列表；仍有重复时继续逐对处理，遵守原有生成和审核预算。
   不凭传递关系新增重复判断。模型可选择任一方，不被原审核的 pass/revise 方向固定。
   Schema 用 enum 限定选项；后置检查拒绝未发放或过期的选项、多个选择和任何额外字段。
   完整列表的数量、成员、原顺序、内容和编号均由程序保证。去重只删除，不编写融合点，
   同时存在的其他问题交复审继续处理。
   没有重复关系时，Harness 把不合格项编译成局部纠错选项。一般问题可修正或撤回；混合机制可拆分。
   只有审核指出明确遗漏且给出已读依据时才发放新增选项。模型按每个问题选择一个 `option_id`。
   对无依据的补充要求，生成器可选择 `request_recheck`，不改候选，仅给出理由交审核复核。
   该请求不能自行消除缺口；同样反馈反复提交会因无变化而停止。
6. 修改以事务方式应用：未经授权的候选不能修改、不能重叠修改、不能用旧选项或虚构来源，
   无实际变化的修改会被拒绝。修改后重新审核全部当前候选，新增项也与保留项一同审核。
7. 合法性检查失败时由固定程序给出错误，加载纠错程序，交回产生错误的角色修正一次。
   JSON schema/enum 同时发送到模型端，但服务可能不严格遵守；后置程序校验是实际安全边界。

Reader 每次重开并验证同一个冻结快照。缺文件、身份不符、路径越界或哈希改变直接停止；
只有未分类的 I/O verification failure 允许一次同版本重试，失败与重试都计入工具预算。
模型不能用新版本、PDF 或其他路径替换出错材料。

## 预算与终态

默认生成 3 次、审核 3 次、阅读选择 3 次，三类模型调用分别计数；纠错调用计入责任角色。
首次生成后的补读选择及其格式纠错共用阅读选择额度，实际补读共用 Reader 字符和段数额度；选择不补读仍消耗一次模型阅读选择调用。
各次调用的 token 用量也写入 trace；当前独立上限是调用次数，不是各角色单独的 token 硬上限。
Reader 默认累计字符上限为当前已核验 `PaperInput.full_text` 字符数的两倍，读取尝试次数上限为分段数的两倍，
足以读取全部分段并为每段留出一次 I/O 重试。`max_read_chars`、`max_read_segments` 为 `null` 或省略时动态计算；
显式正整数仍可设置固定上限。每次读取尝试（含失败和重试）按请求分段长度扣除字符额度，并在执行前检查。
轨迹中的 `read_attempt_chars` 记录累计扣额，`read_chars` 仍表示实际成功取得的不重复原文字符数，
`char_limit`、`reader_limit` 记录本次生效上限及其计算模式。
审核提出补读时，该次模型调用计审核预算，实际读取计 Reader 工具预算。全局模型预算仍由现有运行管理器执行。
累计阅读额度不等于单次模型上下文容量；现有上下文准入限制仍生效。

不会在已无审核预算时生成一个无法复核的新版本。重复错误、重复无效修改、有界纠错失败或
硬限制会停止。预算终止时只返回当前版本仍通过审核的候选；其他候选及未解决问题保留在 trace。
传输或完整性故障使节点失败，已有通过项仍保存在失败轨迹中，不冒充成功交付。

`coverage_complete=false` 与 `semantic_completeness_verified=false` 保持显式边界：
模型认为已读候选通过不等于独立验证了整篇论文的贡献覆盖。允许一个点通过，禁止为达到三个而凑点。
工作流的 `point_coverage` 从 Harness 轨迹投影审核、执行和覆盖状态、未读分段及已读字符数，
不再读取旧流程的特征保留账本。

## 记录与复现

`point_extraction_trace.json` 使用 `extractor_choices_v6` 标识统一 Harness 及完整 digest/content_list 输入协议，`post_generation_reading` 记录首次数量、建议数量、选择与执行或跳过状态。轨迹保存每次角色调用及输入／输出、程序检查结果、审核问题、纠错菜单与
模型选项响应、Harness 组装的完整结果列表、被删 ID，以及其他修正的
实际 patch、候选版本、已通过与未解决候选、Reader 返回正文和范围、独立预算与终态。
正式工作流在根目录、输入快照目录和可用的 runtime diagnostics 中保留该轨迹。

## 配置与迁移

正式工作流在提取前持久化 PaperInput，建立冻结引用并交给 Harness。
直接构造 `NoveltyPointExtractorAgent` 无须显式开启 Harness；调用方必须传入携带 `paper_source` 的 digest。
只有 digest、没有全文快照引用的生产调用会在调用模型之前失败；`DemoPointExtractor` 仍作为独立演示实现。

删除了旧阅读／增量凑点／删除下标去重流程及其专用覆盖账本。
旧 `prompt_names`、`conservative_dedup`、`prompts` 配置不再接受；四种模板统一配置在 `harness` 的
`reader_prompt`、`generate_prompt`、`review_prompt`、`correction_prompt`。
历史实验产物与冻结源码不改写；复现旧协议应使用对应历史快照。

```python
from novelty_agent_framework.config import load_application_config, build_workflow

config = load_application_config(overrides={
    "point_extractor": {"harness": {"generation_calls": 3, "review_calls": 3}}
})
workflow = build_workflow(config)
```

## 验证边界

`tests/test_extractor_controlled.py` 覆盖角色隔离、独立预算、受限修改、补充后复审、
逐对比较一致性、纠错终止、伪造来源、Reader 失败及默认配置注入。
`tests/test_point_paper_reading.py` 覆盖两入口的新进程全文恢复、删除原 PDF 后读取、
来源定位、版本串用和失败轨迹。测试不依赖实验专用 profile、脚本或归档目录。

程序验证来源是否实际读过、修改是否获准、审核输出是否自洽；不能独立保证模型的语义判断正确。
阅读全文能力也不表示模型每次都会选择所有必要分段。
