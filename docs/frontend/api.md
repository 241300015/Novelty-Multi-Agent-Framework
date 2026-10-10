# 当前查新 API

前缀为 `/api/novelty`，由 main.create_app 注册 routers/。废案的 `/api/runs` 和 `/api/health` 已删除。

| 方法与路径 | 功能 |
| --- | --- |
| GET /api/novelty/health | 工作流就绪状态；初始化失败返回 degraded |
| POST /api/novelty/runs | 接收 PaperInput JSON，返回 HTTP 202 和 RunSnapshot |
| POST /api/novelty/runs/files | 接收单个 multipart `paper` PDF，返回 HTTP 202 和 RunSnapshot |
| GET /api/novelty/runs/{task_id} | 查询任务状态、进度、结果和报告地址 |
| GET /api/novelty/runs/{task_id}/report | 返回 Markdown；disposition=inline 或 attachment |
| GET /api/novelty/report-artifacts/{resource_id} | 查询独立恢复报告资源 |

PDF 默认上限 30 MiB；参考文献上传尚未开放。X-Submission-Id 配合文件摘要保护同一提交意图，冲突内容返回 409。

状态为 queued、running、succeeded、failed。progress.stage 包括 parse_paper、extract_points、plan_research、research、validate_evidence、render_report，progress.round 表示补检轮次。API 任务编号是 task_id，不与内部 ResearchTask 的 task_id 混用。

任务状态保存在内存，重启后可能返回 404。服务初始化失败时任务接口返回 503。报告预览和下载读取同一份落盘 Markdown。

当前没有 /artifacts 阶段成果接口；界面已有阶段进度，阶段成果查询和展示尚待接入。
