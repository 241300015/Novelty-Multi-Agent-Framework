# 论文查新 Multi-Agent 框架

这是一个基于 LangGraph 的论文查新辅助工具。系统将论文解析、查新点提取、检索规划、候选文献召回、证据分析、审查和报告生成拆成可审计的阶段。

## 工程认识与后续方向（2026-10-18）

- **流程化选择的 Harness：固定任务检验支持其效率收益。** 采用类似 Skill 的流程组织方式，由 Harness 管理阶段、提供当前合法选项并执行动作，LLM 负责选择和最终草稿组织。当前实现是阶段状态机与受约束的选项协议。对照检验中，这种方式减少了重复读取，降低了 token 消耗、耗时和动作错误率；模型调用次数并未相对原版减少。结论限于本次本地模型和固定任务，阅读量差异也影响成本，仍需更多任务验证。具体流程见[数据链与 Harness 说明](docs/data_flow.md)；详细实验记录保留在实验分支。
- **分层、分节点的错误处理：逐步完善。** 按发生层级、发现节点和可确认的具体原因记录错误码，再选择对应纠错动作，并记录纠错结果与复核结果。Planner 已接入按码定向修复、修改范围复核及失败终止，首轮与补检共用任务合法性门控。其他节点的完整纠错闭环仍需完善：例如引文字符不匹配与把查新对象当成参考文献，应分别识别和处理；贡献归属、比较对象等语义错误不能以通过 Validator 代替语义核验。详见[错误码与恢复机制](docs/error-and-supplement-codes.md)。
- **上下文压缩：待验证的优化方向。** 可以尝试压缩历史对话、已完成动作和重复材料，保留任务目标、当前阶段、关键结论及材料索引，需要时按索引回读原文。压缩后的摘要不能替代用于引文核验的原文、位置和溯源记录。后续应对比 token 消耗、信息遗漏和证据质量，再决定是否正式采用。

## v0.1 Demo

后端已用两篇不同主题的真实 PaperInput 完成完整回归并标记 `v0.1.0-demo`；[实验记录与 Known Issues](docs/experiments/20260917_005448/solution.md)包含两份报告和 Runtime Debug。Demo 前端支持单篇 PDF 上传、阶段进度、Markdown 报告预览与下载；PDF 入口依赖现有 MinerU 配置，[前端运行说明](frontend/README.md)和[部署说明](docs/frontend/deployment.md)列出启动步骤。已有 PaperInput 可使用 `scripts/run_full_workflow_live.py` 直接运行，无需重新解析 PDF。

这是研究原型和辅助工具，不能替代正式科技查新机构。以下工作流与早期实验章节保留了项目演进记录；当前封版结果以 v0.1 实验记录为准。

## 当前工作流

```text
PaperInput
  ↓
PointExtractor                     从论文提取“查什么”
  ↓
Coordinator.plan                   把查新点拆成中英文 ResearchTask
  ↓
任务合法性门控                      核验任务与原始查新点绑定
  ↓
SearchPlanner                      异步生成草稿、有限修复、确定性编译
  ↓
计划完整性屏障                      本批全部规划成功后才派发研究
  ↓
dispatch_research_tasks            按任务动态 fan-out
  ↓
TaskResearcherWorkflow             单任务 Researcher 决定检索、阅读或结束
  ├─ structured_source_retrieval   检索并保存 Work/SourceRecord/Artifact
  └─ reference_artifact_reader     按 Manifest 安全读取原文片段
  ↓
Evidence Compiler                  确定性绑定 quote、Artifact 和字符位置
  ↓
EvidenceValidator                  证据质量门控与去重
  ↓
EvidenceReviewer                   证据复核
  ↓
Provenance Integrity Gate         过滤溯源链不完整的 Card
  ↓
Final Evidence Sufficiency Check  按查新点检查最终有效 Card 数量
  ├─ 数量不足 且轮次未耗尽 → Coordinator.plan_supplement → 重新经过完整检索链
  └─ 数量达标或轮次耗尽
  ↓
Coordinator.synthesize             生成结构化 NoveltyReport
  ↓
Renderer                           生成 Markdown 报告
```

LangGraph 节点：

```text
START
→ extract_points
→ plan
→ plan_research_task               每个任务异步规划；失败停止本批
→ dispatch_research_tasks
→ run_research_task                每个 ResearchTask 运行一个 LangGraph 子图
→ validate_evidence
→ review_evidence
→ validate_synthesis_input
→ check_final_evidence_sufficiency
  ├─ plan_supplement → plan_research_task → dispatch_research_tasks → ...
  └─ synthesize_report → render_report
→ END
```

## 关键架构边界

- Coordinator 只负责任务拆分、补检规划和全局汇总，不生成数据库查询；
- SearchPlanner 的模型只生成概念词项与策略意图；编译器确定性填写任务绑定、编号与布尔表达式，不包含 `all:` 等数据库语法；
- QueryAdapter 是无 LLM、无网络副作用的确定性编译器；
- SearchTool 只执行已经编译的查询；
- 每个 Researcher 只接收一个绑定的 `NoveltyPoint + ResearchTask`，通过模型原生
  Tool Calling 调用注册工具，不接收全局任务或最终报告上下文；
- TaskResearcher 子图负责预算、重复调用限制和局部失败隔离，主 Workflow 负责
  fan-out/fan-in、Validator、补检和终止控制；
- Task 的工作流身份是 `(novelty_point_id, task_id)`，因为 `task_id` 只在单个查新点内唯一。

### StructuredSourceRetrievalTool

`StructuredSourceRetrievalTool` 接收一个 `NoveltyPoint`、一个对应的
`ResearchTask` 和 `source_id`，确定性执行 `SearchPlanner → RetrievalSource →
Metadata/FullText`，保存 `Work / SourceRecord / Artifact`，并返回 Evidence 为空的
`ResearchBundle`。它不包含 Researcher、EvidenceCard、EvidenceValidator、最终证据数量判断
或报告生成。它通过通用 Researcher 工具注册表接入任务子图。

`Final Evidence Sufficiency Check` 在 Validator、Reviewer 和 Provenance
Integrity Gate 之后，对每个 `NoveltyPoint` 的最终有效 `EvidenceCard`
数量进行确定性检查。数量低于系统配置
`min_final_evidence_cards_per_point` 时，记录结构化
`insufficient_final_evidence` 事实。V0 继续使用现有补检回边。该节点仅
判断 Card 数量，不评价检索范围、来源多样性、证据质量或新颖性结论。

arXiv 是当前能力完整的结构化来源，但 Tool 不假设所有来源都能取得全文。Coordinator
分发任务后，由每个 Researcher Agent 决定是否调用该 Tool。WebSearch、Browser 与
Reader 已分别完成真实 Harness/工具链验证，但尚未迁移到正式 Researcher Workflow。

### Researcher Harness 核心原则

```text
LLM = Control Plane
Harness / Tool Runtime = Data Plane
```

LLM 只提供意图参数并选择持久化句柄；Harness/Runtime 注入可信 task scope，恢复
URL、Work 和存储对象，管理基础设施配置与完整审计 Observation，并把最小投影视图
交还模型：

```text
Intent Arguments                         ← LLM
Trusted Runtime Arguments / Resolved Data
Infrastructure Config                    ← Harness / Runtime
```

工具之间使用持久化句柄连接：

```text
WebSearch          → source_record_id
Browser            → artifact_id
Reader             → read_id
EvidenceCardBuilder → evidence / evidence_card
```

模型通过句柄做决策，不承担工具之间的数据搬运。当前已验证 WebSearch、Reader，
Browser 已实现本地 Playwright 后端；Harness 使用 native Model Tool Calling，并将完整
Observation 留在 trace 中，仅把各工具的 model-context projection 放入 `role=tool`。

数据源通过 `RetrievalSourceRegistry` 注册并由 `retrieval.active_source` 选择，Workflow、Agent 与领域 Schema 不包含数据库分支。

生产/实验数据源：

- arXiv（默认启用）
- ScienceDirect（默认禁用；已完成离线 API 契约测试，启用时需要 Elsevier Key）
- Springer Nature（默认禁用；Meta API 检索，开放全文 JATS，TDM 全文需另行授权）
- IEEE Xplore（默认禁用；Metadata API 检索，默认仅尝试 Open Access 全文）

新增数据库的 Provider 边界、配置和测试要求见
[`docs/database-providers.md`](docs/database-providers.md)。

测试数据源：

- `null_catalog`（离线 Null Object，永远返回空结果）

`null_catalog` 只验证数据源注册、配置选择、QueryAdapter/SearchTool 替换和空结果处理；它不模拟真实文献数据库，不验证 FullTextTool、MetadataTool 的跨来源兼容性，也不用于正式查新。用户自制本地数据库导入仍是 `experiments/` 下未实现、未接入工作流的探索功能。

Reviewer 排障可运行：

```bash
python scripts/reviewer_diagnostics.py outputs/<paper_id>
```

该命令只读检查 `novelty-reviews.json`、Card→Evidence 引用闭包、RelevantWork
引用、补检旁路信号，以及 Runtime Artifact 中 Reviewer 的 Reader 调用；输出不会复制
Evidence quote 或 Reader 正文。`review_evidence` 阶段的 `meta.json` 和运行
`summary.json` / `summary.md` 也包含按查新点整理的白名单、漏评、重复评审、未解析
Evidence 与 Card 保留情况。

跨 Namespace 参考文献资产链排障可运行：

```bash
python scripts/reference_namespace_diagnostics.py outputs/<paper_id>
python scripts/reference_namespace_diagnostics.py outputs/<paper_id> \
  --namespace subject_reference --artifact-id <artifact_id>
```

该命令只读检查两个 manifest、Artifact 文件完整性、Reader 输入/输出 namespace
一致性及 Evidence provenance 回指，不输出 Reader 正文或 Evidence quote。跨 namespace
出现相同裸 `artifact_id` 会作为需要 namespace-aware 访问的提示，而不会被判定为错误。

ChinaXiv 接入状态：暂停，未注册为可用数据源。截至 2026-08-14，官网关键词检索仅验证到 HTML 表单，未发现可稳定直接调用的公开关键词 API；OAI-PMH 候选端点从当前网络返回“无权访问”而非 OAI XML，因而无法验证元数据收割、详情元数据和公开 PDF 契约。项目不会以网页爬虫或未经验证的协议假设冒充正式 ChinaXiv 支持。待取得官方接口文档或可稳定访问的 OAI-PMH 响应后再继续实现。

## 项目结构

```text
backend/env/                              统一模型客户端、模型注册表和 PromptLibrary
backend/src/novelty_agent_framework/
├── agents/                              Coordinator、PointExtractor、SearchPlanner、Researcher、Validator、Demo
├── config/                              配置加载和真实工作流组合根
├── diagnostics/                         LLM usage 归一化与 RMB 计费
├── ports/                               可替换能力接口
├── processing/                          PDF 文本层解析、OCR 兜底、章节与标题提取
├── prompts/                             版本化 Agent 提示词
├── schemas/                             Pydantic 数据契约
├── tools/                               Adapter、arXiv 工具、Renderer 等
├── workflows/                           LangGraph 状态和主工作流
├── persistence.py                       按 paper 隔离的本地产物持久化
└── main.py                              FastAPI 入口
docs/                                    架构、进度和实验记录
examples/                                示例论文和 JSON 输入
outputs/                                 本地运行产物，具体生成文件不提交
tests/                                   离线与 live 测试
```

## Paper 工作目录

Renderer 当前交付范围、验收结论及实验资料入口见 [Renderer 交付与验收摘要](docs/Renderer_交付与验收摘要.md)。

```text
outputs/<paper_id>/
├── paper-input/
│   ├── full.md
│   ├── content-list.json
│   ├── images/
│   └── others/paper.json
├── references/
│   ├── list.json                    # 参考作品、来源记录与已保存制品清单
│   └── documents/<work_id>/         # PDF、解析文本等实际制品
├── subject_references/              # 原论文参考文献，含 list.json 与 documents/
├── novelty-points.json
├── retrieval-plans.json
├── candidate-audit.json             # 任务执行状态及实际候选
├── research-runs/<point_id>/<task_id>/attempt-<n>.json  # 读取、构建及补正审计
├── evidence-cards.json
├── novelty-reviews.json             # 单卡／查新点评审、阶段状态及 Gate A
├── recovery/<run_id>/               # 按轮次保存的补检门控和 Coordinator 规划审计
├── report.json
├── report-inputs.json               # 编译门控：运行 ID、文件摘要、依赖版本与材料时间
└── report/<paper_id>-report.md
```

默认报告使用 `backend/src/novelty_agent_framework/templates/markdown/default.md`，
展示论文输入、查新点、规划与实际检索、候选和材料、Validator、评审与 Gate、
局部恢复、任务级补检、结论及限制。文末文件目录按报告保存位置生成附件链接；
全文和详细审计保留在原文件中。文件映射与版本门控集中在 `report_inputs.py`，
阶段展示位于 `tools/renderer_sections.py`。

可以在新进程中只凭落盘目录重渲染，无需启动 Agent 或调用模型：

```python
from novelty_agent_framework.tools.renderer import render_report

render_report(paper_name="paper-id", output_root="outputs")
render_report(paper_name="paper-id", output_root="outputs", template_name="concise")
# 自定义模板／输出位置：template_path="custom.md", save_path="exports/report.md"
```

`template_name="default"` 生成完整报告 `report/<paper_id>-report.md`；
`template_name="concise"` 使用独立的 `templates/markdown/concise.md`，另存为
`report/<paper_id>-concise.md`，两种报告可同时保留。自定义 `template_path`
不与非默认 `template_name` 同时使用。两种模板均通过同一版本门控，只读取已封存材料。

展示版按提供的科技查新报告样例组织为查新目的、科学技术要点、查新点、
查新范围要求、检索范围及策略、检索结果和查新结论；有科学范围或引用限制时才附备注。
文献按 `[1]` 等编号列出，直接显示 URL 或已有元数据，不使用“原文”链接标签。
查新结论优先使用 Coordinator 报告中的 `conclusions[].summary`；没有可用综述时，
才按已落盘的逐点比较与裁定生成兜底内容。历史材料中由程序填入的两种“核验未完成／关键比较证据不足”占位句也按无综述处理。
正式展示版不展示卡片／任务编号、置信度、Gate、补检次数、预算、执行故障、
检查记录及重复附件；未完成的科学裁定仍明确为无法裁定。
不填造样例中的委托人、机构、签章、数据库覆盖年代和检索命中总量。

| 展示字段 | 已核验输入及取值 |
| --- | --- |
| 项目信息、技术要点 | `paper-input/others/paper.json` 的标题、英文标题、摘要；无摘要时取未与查新点重复的贡献说明 |
| 查新点 | `novelty-points.json` 的中文 claim，按落盘顺序编号 |
| 检索来源、检索式 | `retrieval-plans.json` 的实际查询；只有计划时明确标注未记录执行结果；检索词取论文关键词 |
| 相关文献 | `evidence-cards.json` 的最终有效非网页证据，按明确 Work 身份或来源链接合并，保留标题、贡献与原文链接 |
| 查新结论 | 优先展示 `report.json` 的 `conclusions[].summary`；缺失或为程序占位句时才使用 feature_comparisons、highly_relevant_works 与最终裁定兜底，不自行重写综述 |
| 备注（可选） | 保留科学范围限制、缺失文献／对比方法及引用问题；已知执行诊断格式以及恢复、停止、完整性检查记录只留在完整报告 |

编制日期是本次编译日期。展示版不列全量候选、原文引句和调试轨迹，
这些仍可从同一材料的 default 报告查看。参考样例对齐说明、样稿和复现入口见
[P0-07 展示模板实验](docs/experiments/20261019_p0_07_concise/README.md)。

检索语言与解释语言独立：文献范围和查询词可以是英文，模型生成的综述、贡献说明、
异同比较、理由和解释统一要求简体中文。`response_language.py` 在 Agent／Harness
实际模型调用前追加要求，覆盖模板与内置备用提示词。文献题名、专有名称、原文引句、
URL／DOI、字段名／枚举／ID 和明确要求英文的字段保持原样。
该约定作用于新的模型请求；重渲染历史报告不调用模型，不翻译或篡改历史事实。

编译必需输入为 `paper-input/others/paper.json`、`novelty-points.json`、
`retrieval-plans.json`、`evidence-cards.json` 和 `report.json`，以及 `report-inputs.json` 版本记录。
其他阶段产物缺失时报告明确展示缺失状态；存在但无法解析或论文身份冲突的 JSON 会阻断编译。
编译不会补建输入文件。提取／去重中间轨迹和 Gate B 目前没有统一工作区入口，
报告保留这一限制；局部恢复建议不作为已执行、已复核的事实。
进入模板编译前，版本门控核对运行 ID、文件 SHA-256 和阶段依赖版本。
版本在各阶段持久化时登记，`persist_workflow_input()` 开始新运行，`persist_report()` 封存材料；
Renderer 只消费通过核验的同一批文件字节。缺少记录、材料尚未封存、混入旧阶段产物、
文件被改写或上游版本已变化都会阻断编译，并保留已有报告。
工作流自动传递本次运行 ID；独立重渲染也可通过 `expected_run_id` 指定预期运行。
文献目录允许作为已有输入复用，在封存时登记目录版本；这不把旧阶段成果重新认定为本次产物，
也不替代 Reader／Validator 对文献正文和引用的核验。

报告中“报告生成时间”为当前编译时间，“材料所属时间”为本次材料登记开始至封存的区间，
均使用带时区的 UTC 时间；后者来自落盘版本记录，重渲染时保持不变，不代表文献发表时间。
没有版本记录的旧工作目录需要重新执行持久化流程，不根据文件修改时间猜测版本或时间。
需要保留同一论文的多次运行时，仍应使用不同 `output_root`；版本门控负责拒绝混用，不负责归档。

真实实验还可额外写出 `run-metrics.json`，记录墙钟时间和逐次模型 usage。

开启 `runtime_debug` 时，每次生产模型调用还会写入
`outputs/<paper_id>/runtime/<run_id>/llm_calls/`。单次记录包含模型、阶段、耗时、
输入/缓存输入/输出/推理 token、所用单价档及 RMB 金额；`summary.json` 和
`summary.md` 提供按模型与整次运行的汇总。单价表位于
`backend/src/novelty_agent_framework/config/llm_pricing.json`，未知单价会标为
`UNPRICED`，不会按 0 元处理。

`retrieval-plans.json` 按查新点保存 `research_tasks`、数据库无关 `search_plans`、真正执行的 `executed_queries`，以及供现有 Renderer 使用的 `query_plan.queries`。

## 环境和安装

项目使用已经创建的 `Novelty` Conda 环境：

```bash
conda activate Novelty
pip install -e ".[dev,web,browser]"
python -m playwright install chromium
```

真实模型调用使用 `backend/.env` 或进程环境变量：

```dotenv
SILICONFLOW_API_KEY=...
```

`backend/.env` 已被 Git 忽略。不要把真实 Key 写入 `.env.example`、代码、文档或提交历史。

## 运行方式

### 离线 Demo

离线 Demo 使用确定性的 `DemoTaskResearcher` 跑通任务 fan-out、Validator、补检和汇总，不访问网络：

```bash
novelty-demo --input examples/paper.json --output /tmp/novelty-result.json
```

### PDF 处理

PDF 解析默认优先使用 MinerU（独立 `mineru` conda 环境，Python API 桥接），
MinerU 不可用或质量不足时自动回退到原有文本层 + DeepSeek-OCR。

```bash
# 先准备 MinerU 独立环境（只需一次）
conda create -n mineru python=3.11 -y
conda activate mineru
pip install -U "mineru[core]==3.4.5"

# 默认走 MinerU，失败自动回退
python -m novelty_agent_framework.processing.cli \
  --input examples/MF2033k6lC.pdf \
  --output outputs

# 强制走旧文本层/OCR
python -m novelty_agent_framework.processing.cli \
  --input examples/MF2033k6lC.pdf \
  --output outputs --parser text_layer
```

MinerU 解析出的图片、表格、公式会以结构化 JSON 保存到
`outputs/<paper_id>/paper-input/others/paper.json` 和
`outputs/<paper_id>/paper-input/content-list.json`：

```json
{
  "images": [
    {"image_id": "image-1-1", "kind": "image", "page": 1, "path": "images/xxx.jpg", "caption": "图 1", "footnote": "", "bbox": []}
  ],
  "tables": [
    {"table_id": "table-1-1", "page": 1, "caption": "表 1", "footnote": "", "body": "<table>...</table>", "body_format": "html", "bbox": []}
  ],
  "equations": [
    {"equation_id": "equation-1-1", "page": 1, "latex": "x = y", "bbox": []}
  ]
}
```

图片文件会复制到 `outputs/<paper_id>/paper-input/images/`，JSON 中的 `path`
是对应 paper 工作区的相对路径（如 `images/000_xxx.jpg`）。

### 配置驱动的真实工作流

```python
from novelty_agent_framework.config import build_workflow, load_config
from novelty_agent_framework.schemas import PaperInput

config = load_config()
config["retrieval"]["sources"]["arxiv"]["enabled"] = True
workflow = build_workflow(config)
result = workflow.run(PaperInput.model_validate(paper_data))
```

当前默认配置已将 Coordinator、PointExtractor、Researcher、SearchPlanner、Reviewer
以及文本处理的 LLM 兜底统一为 `local-qwen2.5-7b`；OCR 保留专用的
`deepseek-ocr`。仍可分别通过 `NOVELTY_COORDINATOR_MODEL`、
`NOVELTY_POINT_EXTRACTOR_MODEL`、`NOVELTY_RESEARCHER_MODEL`、
`NOVELTY_SEARCH_PLANNER_MODEL`、`NOVELTY_REVIEWER_MODEL` 和
`NOVELTY_PROCESSING_LLM_MODEL` 显式覆盖。

正式配置默认启用 Extractor 生成／审核 Harness、Researcher 受控阅读与纯选项交互、
Planner 有界修复、Coordinator 分触发码补检及 Renderer 输入版本门控。
Researcher 默认使用 `research/choice_only`，分轮预算随受控阅读启用。
历史实验 profile 的显式覆盖及 checkpoint 兼容边界见 [配置说明](config/README.md)。

### Renderer 与 API

```python
from novelty_agent_framework.tools import render_report

path = render_report(output_format="markdown", paper_name="paper-id")
```

```bash
python -m novelty_agent_framework.main
```

## 测试状态

2026-10-18 完整非 live 回归：1577 项通过，3 项因缺少旧 Reviewer 实验原文归档而跳过，
6 项 live 测试未运行。包含 API 生命周期、Planner 异步规划与定向修复、任务绑定门控、
越界后的字段限定纠正、整批失败停止及既有检索回归。

同日首批 Planner 本地真实模型验证：`qwen2.5-7b-instruct` 实际调用 22 次，
13 个场景检查符合预期；5 次受控草稿修复成功，越界修改被拒绝，整批失败时派发数为 0。
故障在真实响应上显式注入，修复也调用真实模型；结论仅覆盖两篇论文的固定查新点与规划边界，
不代表完整查新链路、自然错误率或成本收益已验证。原始记录留在本地实验分支
`experiment/planner-live-20261018`，不合并到 main。

随后补充越界后的受限纠正：程序固定可改字段，从原稿组装，最多纠正一次且计入原总预算。
最终版本用同一本机模型实际调用 23 次，7 个场景检查符合预期，包括四种字段修复及成功/失败批派发。
探索另调用 15 次，发现一次空概念集合并正确拦截，补清集合说明后复测通过。
包括该失败样本在内的原始记录只保留在本地分支 `experiment/planner-scope-correction-20261018`。

```bash
pytest -m 'not live'
pytest -m live tests/test_live_arxiv_tools.py -s
```

## 第一次真实原型实验

`examples/MF2033k6lC.pdf` 已完成一次成功的两轮真实流程：

| 指标 | 结果 |
| --- | ---: |
| 总耗时 | 1,076.034 秒（约 17 分 56 秒） |
| 模型调用 | 19 次 |
| 输入 / 输出 / 总 Token | 145,566 / 93,509 / 239,075 |
| 理论模型费用 | 约 ¥0.33 |
| 原始 / 接受 / 拒绝证据 | 11 / 6 / 5 |
| 最终覆盖 | NP-1 partial；NP-2、NP-3 insufficient |

详细记录见：[第一次原型机实验测试结果](docs/issues/第一次原型机实验测试结果.md)。

## 当前限制

- 只有 arXiv，中文文献覆盖不足；
- Planner 的编号与绑定由程序组装，草稿修复有次数和字段范围限制；固定样本本地实测已完成，跨任务修复成功率仍需验证；
- Planner 已支持异步规划；其他节点和工具的并发能力需分别验证；
- Coordinator 补检和汇总上下文过大，是主要输入 Token 来源；
- V4-Flash reasoning token 较高；
- 默认 Coordinator 模型当前不可用；
- 第二轮 EvidenceValidator issue 会重复累计；
- 本地 JSON 和进程内任务存储仍是原型实现。

## 文档

- [代码框架说明](docs/code-framework.md)
- [代码框架详细说明](docs/code-framework-detailed.md)
- [框架演进总结](docs/框架演进总结.md)
- [开发进度](docs/开发进度.md)
- [第一次原型机实验测试结果](docs/issues/第一次原型机实验测试结果.md)
