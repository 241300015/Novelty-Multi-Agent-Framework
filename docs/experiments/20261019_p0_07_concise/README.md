# P0-07 展示模板与离线对照

2026-10-19，分支 `feat/p0-07-concise-report`。按用户指定的工作区参考报告
`docs/BA72DB5299DC6262C43E75283FA_09A94BC5_291F99.pdf` 设计展示模板。
样例第 3–9 页的项目说明、检索结果及对比结论是结构依据；不复制样例的科研内容、
机构／联系人、签名印章或检索数量。原 PDF 保持原状，未纳入本实验快照。

展示版保留样例第一至第七节结构，仅在存在科学范围或引用限制时附“八、备注”。
文献编号取代内部卡片、Work 和证据编号；正文不列置信度、Gate、阶段目录、
读取位置、错误码及恢复流水。相同文献合并，技术概述优先使用摘要，避免重复查新点。
文献直接呈现 URL 或已有元数据，不加“原文”超链接标签。
查新结论优先展示 Coordinator 的已落盘 summary；没有综述时才使用已有的比较与裁定兜底。
历史样本的 summary 是程序生成的失败占位句，因此这四份样稿展示的是兜底路径，
不能将其当作 Coordinator 实际生成的综述。主路径由构造数据验证 summary 优先于评审理由和逐特征比较。新运行成功生成的 summary 在证据不足时也保留，
通过 synthesize.md 提示词约束其证据边界；绑定逻辑继续保留 Reviewer 的结构化裁定，不将其升级为新颖。
正式展示中不出现补检上限、执行故障、预算、恢复流水和完整性检查记录；这些仍在 default 与原始产物中保存。
科学范围／引用限制保留，已知上游诊断格式不再列入备注。

新模型请求要求中文综述及解释，英文检索词、文献元数据和原文引句保持原样。
最初的离线验证覆盖文件模板、备用提示词及 Harness 的实际请求构造；后续已完成
[本地模型真实测试](live-local/REVIEW.md)，中文输出生效，但综述的证据边界未通过验收。
离线重渲染不翻译历史文献贡献和评审说明，因此历史样稿仍可能含有英文解释。

## 样稿与结果

| 材料 | 查新点 / 有效卡 | 完整版 | 展示版 | 字符减少 |
| --- | --- | --- | --- | --- |
| 历史 A | 2 / 0 | [default](results/case-1/default.md) | [concise](results/case-1/concise.md) | 81.4% |
| 历史 B | 4 / 0 | [default](results/case-2/default.md) | [concise](results/case-2/concise.md) | 84.4% |
| 已完成落盘 01 | 3 / 1 | [default](results/case-3/default.md) | [concise](results/case-3/concise.md) | 87.0% |
| 已完成落盘 02 | 4 / 3 | [default](results/case-4/default.md) | [concise](results/case-4/concise.md) | 75.5% |

每份材料均在两个独立子进程中各编译两种模板，业务内容保持一致；原材料及索引不变。
占位符和 default 本地附件链接检查通过。封存后修改证据文件，两种模板均拒绝编译，
已有报告保持原样。实际编译输入的文件字节和索引保存在各 case 的论文目录下；
原始文献 PDF 不在本实验包中。外部链接仅验证生成格式，未联网检查可达性。
运行结果及源码摘要见 [results.json](results/results.json)，测试日志见 [tests.txt](tests.txt)。

四份材料均来自此前 Renderer 验收副本，历史产物的版本索引曾由该验收补建；
本实验保留这些索引原样，不重建索引或冒充当前工作流的新运行。
这些报告的最终裁定均未完成；不能用报告缩短或成功编译证明科学判断正确。
有综述的主路径、无综述兜底、同文献跨点去重、比较编号及技术信息不进入展示的要求由构造数据测试覆盖。

## 复现

在仓库根目录、已安装项目依赖的 Python 环境中执行。以下使用已保存快照，
新结果写到 outputs，不覆盖实验记录：

```bash
python docs/experiments/20261019_p0_07_concise/run.py \
  --workspace docs/experiments/20261019_p0_07_concise/results/case-1/MF2033k6lC \
  --workspace docs/experiments/20261019_p0_07_concise/results/case-2/MF2033k6lC \
  --workspace docs/experiments/20261019_p0_07_concise/results/case-3/MF2033k6lC \
  --workspace docs/experiments/20261019_p0_07_concise/results/case-4/MF2033k6lC \
  --destination outputs/p0-07-replay

python -m pytest tests/test_renderer_concise.py tests/test_renderer.py \
  tests/test_renderer_versions.py tests/test_persistence.py \
  tests/test_run_full_workflow_live.py tests/test_response_language.py \
  tests/test_report_narrative_contract.py tests/test_report_binding.py \
  tests/test_coordinator_json_robustness.py tests/test_tool_call_harness.py \
  tests/test_evidence_reviewer.py tests/test_researcher_prompt_policy.py \
  tests/test_point_extractor.py tests/test_search_planner.py \
  tests/test_extractor_controlled.py tests/test_task_researcher_workflow.py \
  tests/test_research_agent_tools.py -o addopts='' -q

python -m pytest tests/test_coordinator_choice_harness.py -o addopts='' -q
```

本次回归 **280 + 28 passed**，日志分别为 [tests.txt](tests.txt) 和 [coordinator-tests.txt](coordinator-tests.txt)。
后一组使用工作线程，沙箱内的最小 asyncio.to_thread 示例同样在退出时挂起，因此该组在解除沙箱限制后通过。
上述回归验证使用本地固定数据／假模型，不包含完整 Agent 流程重跑。
后续真实测试使用本机 Qwen2.5-7B-Instruct，4 组共 7 次调用；详细结果见
[真实测试记录](live-local/REVIEW.md)。
API 用法和字段映射见 [README](../../../README.md#paper-工作目录)。
