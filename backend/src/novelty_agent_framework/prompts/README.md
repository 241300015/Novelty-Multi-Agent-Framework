# Prompt 资源

该目录保存生产 Agent 的版本化 Prompt。Prompt 应引用 `novelty_agent_framework.schemas` 中的数据契约，不在路由或 LangGraph 节点中拼接。

- `coordinator/`：任务分配（plan）、补充检索和最终汇总；
- `extractor/`：`reader_choices` 选择补读，`generate` 生成／修复／选择去重保留项，`semantic_review` 审核，`correct` 格式纠错；
- `reviewer/review_evidence.md`：基于多 Work 证据的查新点级新颖性判定；
- `research/`：单个文献调研任务。

每个模板由 front matter（`name` / `version` / `system`）和正文模板组成，
正文通过 `{变量名}` 占位符接收业务数据，schema 由
`NoveltyBrief.model_json_schema()` 等动态注入，保证与 Pydantic 契约同步。

模板由 `backend.env.PromptLibrary` 加载渲染，真实 Agent
（`NoveltyCoordinatorAgent` / `NoveltyResearchAgent`）在装配时注入
PromptLibrary；文件缺失时回退到 Agent 内的默认提示词。
Demo Agent 不读取本目录，仍用于验证工作流闭环。

Extractor 统一使用 Harness 的四个模板，缺失时直接报错；不再使用旧 `read_paper`、
`extract_points` 或 `review_points` 模板。模型输入包含完整 `digest` 与 `content_list` 分段目录，
实际读取由 Harness 根据已发放选项执行。

解释语言统一要求简体中文，独立于 `ResearchTask.language` 所指定的检索语言。
`novelty_agent_framework.response_language` 在生产 Agent／Harness 的模型调用边界
追加要求，文件模板和内置备用提示词均生效。综述、贡献、异同和解释使用中文；
检索式仍按任务语言编写，文献题名、专有名称、原文引句、URL／DOI、JSON 字段／枚举／ID
以及 `claim_en` 等明确要求英文的字段保持原样。该要求不翻译历史落盘材料。
