> 历史废案验收记录：本文描述的原生 TypeScript、/api/runs、单线程 RunStore 和 PWA 方案已退出，不代表当前 React 前端。当前接口与部署以 api.md、deployment.md 和 frontend/README.md 为准；本次删除验证记录见 ../experiments/20261010_web_retirement/。

# 验收记录（2026-09-17）

## 结论

已实现 Phase 0～4 对应代码并完成离线集成、生产构建及 Chromium 页面验证。**尚未完成真实模型端到端验收、桌面 Chrome／Edge 安装验收和服务器 HTTPS 验收，不能宣称任务书所有验收项通过。**

## 已实现

- frontend 分支；固定 frontend/ 根目录；后端核心工作流不改写。
- 单 PDF 选择／拖拽／移除，25 MiB 前后端限制，重复字段／多文件／伪 PDF／加密／损坏输入拒绝。
- 生产配置的 PDF processor → PaperInput → reference bootstrap → standard full workflow → Renderer。
- 独立 Run 存储、后台线程执行、持久化状态、重启中断标记、URL 恢复。
- 非重叠轮询，后端终态停止 Run 查询；连接／任务／报告错误分离。
- 原始 Markdown 预览与同字节下载；禁用原始 HTML、危险协议链接和远程图片。
- 节点完成后发布阶段成果，保留原始裁定和不足提示。
- FastAPI 同源静态托管；PWA manifest、图标、静态缓存与主动更新按钮。

## 已测试及证据

| 检查 | 结果 |
| --- | --- |
| `pytest tests/test_pdf_web.py tests/test_api.py tests/test_workflow.py tests/test_workflow_reviewer.py` | 31 项通过 |
| `frontend: npm test` | 3 项通过：轮询无重叠、终态／取消停止、Markdown 安全及格式 |
| `frontend: npm run build` | TypeScript + Vite 生产构建通过；dist 已生成且忽略提交 |
| npm 依赖安装审计 | Vitest 更新到 4.1.11 后 0 vulnerabilities |
| FastAPI TestClient | 新 API 与原 API 回归通过；报告预览／下载／源文件字节一致，路径隔离、重启和错误契约通过 |
| 真实 PDF + 离线工作流 | `examples/MF2033k6lC.pdf` 进入现有文本解析器、图与 Renderer；替换 Demo agents，禁用外部参考服务；全部阶段成果可发布 |
| Headless Chromium，localhost | 上传、刷新恢复、连接中断提示、报告读取失败保持 completed、重试、报告下载通过；无 pageerror |
| 桌面 1440×1080／移动 390×844 | 页面可用，移动端无横向溢出 |
| Service Worker | 注册及接管成功，只缓存静态 shell，无 /api 请求缓存；离线重新打开可见后端不可用提示 |

浏览器脚本：`scripts/check_frontend_browser.py`。本次运行编号 `run-816be3a4751b49d088cd9c9cb2a78b7f`，本地临时 Run 目录完成后已清理，不是生产查新记录。脚本用已有真实报告作为展示 fixture：

`outputs/ARXIV_PAPERINPUT_RECHECK_20260916T111244Z/runtime/full/MF2033k6lC/report/MF2033k6lC-report.md`

该已有报告只用于测试执行器的格式与下载验证，**生产接口不会回退或复用该旧报告**。截图本次保存为 `/tmp/novelty-desktop.png`、`/tmp/novelty-mobile.png`，不作为仓库业务产物。

环境：Python 3.11.15、Node 22.16.0、Vite 6.4.3、Vitest 4.1.11。Python multipart 安装于 /tmp 隔离测试环境，复用现有 Novelty Conda 依赖。TestClient 在受限沙箱内的事件循环唤醒受阻，纯本地测试经自动审批后在沙箱外通过。Chromium 使用已有 Conda 动态库。

## 未测试／阻塞

1. **真实 HTTP PDF → 外部模型／检索 → 本轮报告**：尝试启动时自动审批拒绝执行。理由：`examples/MF2033k6lC.pdf` 及派生内容可能发送到配置的外部模型／检索服务并产生费用，审批器认为需要针对该负载和目的地的明确授权。没有绕过拒绝，没有产生真实运行编号，也没有以旧报告冒充本轮产物。待用户明确授权后执行并追加记录。
2. 桌面 Chrome／Edge 安装、独立窗口图标启动及更新交互：已实现配置，但本次仅验证 headless Chromium 的 Service Worker，尚未手工安装验收。
3. 服务器 HTTPS：没有可用验收部署目标，未发布服务器、未修改反向代理。

## 已知限制／暂缓

- 单进程、单执行线程；不能多个 workers 共享 Run 目录。服务重启仅标记中断，不续跑。
- 不支持取消任务、历史列表、用户权限、公共上传服务、非根路径部署。
- Markdown 图片、公式及相对资源不作为首版支持承诺；表格、标题、列表、引用、链接和正文已验证。
- MinerU／模型凭据与检索可用性仍取决于现有后端部署配置。
- 完整真实闭环未通过前，Phase 1/2 的真实运行验收项仍保持未完成。
