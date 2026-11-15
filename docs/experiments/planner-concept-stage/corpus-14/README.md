# 新增 14 篇论文的 Planner 输入样例

录入日期：2026-11-16。14 篇 PDF，共 16 个候选查新点。每点均含中文与英文 claim、技术特征、源 PDF 物理页码。按论文摘要及相关方法页人工整理；英文为整理者表述，不标作作者原句。

这些记录用于给 Planner 提供真实论文输入，不代表已证明创新性，也未执行真实 Qwen 调用或用户人工语义签收。应用研究按实际技术组合录入，未把采用已有架构写成新架构。原有 GSAERU、Museformer 输入保持不变。

## 文件

- `manifest.json`：14 篇论文的标题、PDF 路径与 SHA-256、输入路径、查新点编号。
- 每篇目录的 `novelty-points.json`：现有 `scripts/run_planner_concept_case.py` 可直接读取的输入；仅使用既有 NoveltyPoint 字段。
- 每篇目录的 `source-pages.json`：全部 PDF 页面提取文本，页号从 1 开始，为物理页号。
- 每篇目录的 `evidence.json`：查新点字段的支持页、逐字保存的整页原文及字符偏移、贡献类型、未运行/待签收状态。证据粒度为页面，尚非逐句支持映射；PDF 提取文本可能包含断词、全角字符和公式噪声。该文件不传入模型。
- `index.md`：供人工审阅的全部查新点与技术特征。
- `validation.json`：本次离线数据校验结果。

没有录入标准概念答案或期望别名列表，避免后续把期望输出混进模型输入。为当前脚本准备的数据不等于实现了 V2 证据 ID 协议。

## 运行一个真实 Qwen 概念阶段案例

在项目根目录、项目 Python 环境及 Qwen 服务已配置好的条件下：

```bash
PYTHONPATH=.:backend/src python scripts/run_planner_concept_case.py \
  docs/experiments/planner-concept-stage/corpus-14/04-LightGCN/novelty-points.json \
  --point NP-1 --scenario A --repeat 1 \
  --max-model-calls 24 \
  --output outputs/corpus-14-qwen/lightgcn-np1-A-r1
```

替换输入路径与查新点编号即可使用其他样例；为每次运行指定独立输出目录。

现有脚本场景：A 为中文输入/中文任务，B 为中文输入/英文任务，C 为英文输入/中文任务，D 为中英文输入并分别运行中文和英文任务。16 点覆盖 A–D 共需 64 次命令；D 每次含两个语言任务，因此合计 80 个语言任务，共享各次命令的调用预算。这里仅说明覆盖规模，未启动批量运行。

建议后续先固定代码、Prompt、配置和模型版本，对全部 16 点各运行一次 A 场景；保留请求响应、契约失败、预算及语义签收结果，再扩展其他语言场景。每点的原文与技术特征可在 index.md 和 evidence.json 中复核。
