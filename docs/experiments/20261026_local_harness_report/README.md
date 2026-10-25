# 本地模型、Extractor Harness 与双模板报告实验（2026-10-26）

本目录仅保留在 `feat/p0-07-concise-report`，不合入 main。代码按运行约束修复、模型切换确认、Extractor 迁移、报告导出四个阶段提交；早先 P0-07 的原始实验继续保留在同分支的 `../20261019_p0_07_concise/`。

## 归档内容

`artifact-index.json` 列出七组运行、压缩包校验值及代码版本；`source-files.json` 逐文件记录原始路径、大小和 SHA-256。压缩包保留各运行目录中的原始请求、响应、启动源码/配置快照、输入、阶段产物、报告和审计脚本，仅排除 Python 字节码缓存。`summaries/` 提供可直接浏览的审计结果和报告副本。原始文件仍保留在本地 outputs。

在空目录解压，例如 `tar -xzf full-run-context-20261026-032836.tar.gz -C /tmp/novelty-audit`，得到原来的 `outputs/<run>/...` 结构。部分快照和脚本含运行时的绝对路径，重新执行前需调整；归档不包含服务凭据、运行中的模型或依赖环境。不修改旧轨迹以适配最终代码。

## 最新完整运行的实测结论

`full-run-context-20261026-032836` 从 MF2033k6lC 全文输入运行到报告，97 次模型调用全部使用本地模型；95 次成功，2 次上下文溢出。两次均发生在 Researcher 累积 Reader 正文后的模型请求，分别为 30095 + 4096 = 34191 tokens、30593 + 4096 = 34689 tokens，超过 32768 窗口。对应任务保留局部失败记录且未产生证据卡。Synthesis 两次调用未发生溢出。详细失败调用、任务中断和输出缺失见 context-audit、context-calls、additional-findings。

17 篇调研文献中，2 篇获取了正文（完整性范围仍待核验），15 篇未尝试获取正文。源论文参考文献缓存 89 条成功、2 条历史未找到；这两条不是本轮新增下载失败。全文未获取状态见 fulltext-audit、fulltext-status 和 subject-reference-status。

Extractor 生成 3 个候选，但只补读了 4000 字符，仍有未读分段。第 3 点可能将实验效果独立成点；最终仅有 2 张证据卡且指向同一篇空气质量 DSGNN 文献，存在缩写同名误匹配及不充分证据支撑的问题。流程完成不等于语义质量验收通过；详见 additional-findings.json。

最新 concise 报告已从同一封存产物重渲染，使用 Planner 的检索策略而非执行的 arXiv 检索式；未新增模型调用。原报告备份及重渲染校验记录见 report-export-backups 和 report-exports.json。

## 最终代码验证

完整后端回归：1718 passed、11 skipped（47.20 秒）；另有依赖库弃用提示。前端：10 项测试通过，TypeScript/Vite 构建通过，仅有打包体积提示。完整日志见 validation。覆盖状态测试已从移除的 pending_dedup 迁移到 pending_coverage，验证未读段落、读取量和语义覆盖限制继续传递。

main 隔离集成版本再次通过完整回归：1718 passed、11 skipped（44.92 秒），没有依赖本轮分支实验目录；日志见 `validation/main-pytest.txt`。提交映射和 main 合并提交见 `artifact-index.json`。
