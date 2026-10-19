# Extractor 受控生成、语义审核与纠错

正式工作流默认启用 `point_extractor.harness.enabled=true`，默认模型为本地 Qwen。
配置入口为 `backend/src/novelty_agent_framework/config/agents/point_extractor.example.json`；
可通过应用配置覆盖预算和提示词。模型参数默认 temperature=0、max_tokens=4096。

## 职责与入口

`NoveltyPointExtractorAgent.extract()` 在 `point_extractor.harness.enabled=true` 时交给
`core/extractor_harness.py`。必须提供被冻结索引绑定的持久化 PaperInput；不调用 PDF、MinerU 或 OCR，
不以调用方内存正文替代快照。正式工作流仍负责持久化输入、成果及成功／失败轨迹。

生成和语义审核使用不同系统提示词。生成负责首次提出候选和执行获准修改；审核负责原文支持、
贡献边界、语义重复、混合机制、引用及已读材料中的明确遗漏。审核不能直接改写候选。
Reader 是确定性的工具；阅读选择提示词只是选项入口，没有文件路径或自由偏移参数。

| 用途 | 配置字段（位于 point_extractor.harness） | 模板 |
| --- | --- | --- |
| 生成／按反馈修改 | generate_prompt | prompts/extractor/generate.md |
| 语义审核 | review_prompt | prompts/extractor/semantic_review.md |
| 首次阅读选择 | reader_prompt | prompts/extractor/reader_choices.md |
| 局部纠错程序 | correction_prompt | prompts/extractor/correct.md |

四个模板都归属 Extractor，并纳入启动提示词摘要与版本记录。旧 `reviewer/review_points.md`
仅由保留的旧流程使用，不作为新流程的语义审核模板。

## 执行与限制

1. Harness 校验 PaperInput 索引、身份和哈希，发放绑定本次输入的阅读选项。
2. 生成前显式询问是否需要阅读全文或补读分段。每次只选一个选项；预算允许时有 `read_all`，
   否则选择一个具体分段。读取后提供真实正文和版本化引用。
3. 首次生成最多 8 个候选，没有最低数量要求。编号由程序分配，模型只能引用已读位置。
4. 审核须覆盖全部当前候选，返回通过项、不合格项及原因、原文依据。需要补读时不能同时裁定；
   审核只能选择程序发放的未读选项。省略可选 `read_requests` 表示没有工具请求，不代表补全阅读。
   对每个候选对还必须显式判断 `duplicate/distinct/uncertain`，并与逐点审核结论一致；
   不确定时不能同时通过两点。程序只验证判断的完整性与一致性，不替代语义判断。
5. Harness 把不合格项编译成局部纠错选项。一般问题可修正或撤回；混合机制可拆分。
   重复项只允许撤回或合并，禁止通过换词制造独立性；重复关系不能形成环。
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
各次调用的 token 用量也写入 trace；当前独立上限是调用次数，不是各角色单独的 token 硬上限。
Reader 工具最多 24 次实际读取尝试、累计 20000 字符。审核提出补读时，该次模型调用计审核预算，
实际读取计 Reader 工具预算。全局模型预算仍由现有运行管理器执行。

不会在已无审核预算时生成一个无法复核的新版本。重复错误、重复无效修改、有界纠错失败或
硬限制会停止。预算终止时只返回当前版本仍通过审核的候选；其他候选及未解决问题保留在 trace。
传输或完整性故障使节点失败，已有通过项仍保存在失败轨迹中，不冒充成功交付。

`coverage_complete=false` 与 `semantic_completeness_verified=false` 保持显式边界：
模型认为已读候选通过不等于独立验证了整篇论文的贡献覆盖。允许一个点通过，禁止为达到三个而凑点。

## 记录与复现

`point_extraction_trace.json` 保存每次角色调用及输入／输出、程序检查结果、审核问题、纠错菜单与
实际 patch、候选版本、已通过与未解决候选、Reader 返回正文和范围、独立预算与终态。
正式工作流在根目录、输入快照目录和可用的 runtime diagnostics 中保留该轨迹。

## 配置与兼容入口

正式工作流在提取前持久化 PaperInput，建立冻结引用并交给 Harness。
如果业务代码直接构造 `NoveltyPointExtractorAgent`，须显式传入
`harness_config={"enabled": True}` 及带 `paper_source` 的 digest，才能启用此契约。
直接构造时未提供 Harness 配置，或应用配置显式设置 `enabled=false`，仍使用兼容流程。
兼容流程的 `prompt_names`、按数量补充及旧去重配置不影响已启用的 Harness。

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
