# 当前前后端接入架构

当前 React 前端采用 `3e086f` 的工作成果，后端在 `a7ff34fa` 中与之集成。废案前端对应的 `web/` 适配层及 `/api/runs` 接口已退出。

## 调用链

```text
frontend/src/api/client.ts
  → /api/novelty/runs/files（PDF）或 /api/novelty/runs（PaperInput）
  → routers/runs.py
  → services/NoveltyWorkflowService
  → processing/DefaultPaperProcessor（仅 PDF）
  → workflows/NoveltyWorkflow
  → 证据、审查结果、结构化报告和 Markdown 落盘
```

路由通过 BackgroundTasks 发起任务。单次工作流通过 LangGraph Send 和审查并发执行任务，实际并发上限来自配置。没有废案的单线程任务工作池。

## 状态与展示

当前任务状态和报告路径关联保存在 services/jobs.py 的 InMemoryRunStore 中，重启后 HTTP 任务记录失效。业务产物仍保存到独立任务目录。任务状态持久化和阶段成果查询/展示是后续工作，不在此次废案删除范围内。

前端轮询真实 progress，显示当前阶段和补检轮次；最终报告由既有 Renderer 生成，预览与下载读取同一文件。当前没有运行中的阶段成果查询接口。

独立恢复报告由 services/report_resources.py 和 /api/novelty/report-artifacts 提供，不依赖已删除的 web/ 包。

## 验证

删除前后使用同一 PaperInput、私人 DeepSeek 官方 API 和并发上限 4 验证；具体结果见 ../experiments/20261010_web_retirement/。真实云端验证不覆盖 PDF/OCR 和浏览器 UI，离线 API 测试另覆盖 PDF 文本解析、静态托管及任务生命周期。
