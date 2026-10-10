# 当前服务开发与运行

从仓库根目录操作，使用 Novelty Conda 环境。

```bash
conda activate Novelty
python -m pip install -e '.[web,dev]'
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..
python -m novelty_agent_framework.main
```

前端运行环境和构建要求以 frontend/README.md 为准。main.py 在 frontend/dist 存在时托管构建结果；没有构建目录时 API 仍可运行。

## 云端配置

backend/.env 保存凭据，文件不提交。私人官方 DeepSeek 运行配置为：

```text
NOVELTY_EXPERIMENT_PROFILE=config/profiles/private-cloud-workflow.json
DEEPSEEK_API_KEY=<在本地填写>
```

该 profile 的查新角色及标题模型使用 DeepSeek 官方 API，并发上限为 4，全局模型调用上限为 240，Coordinator 显式关闭默认思考模式。角色环境变量仍可覆盖 profile；PDF/OCR 配置沿用基础配置。

硅基流动可选择 config/profiles/cloud-workflow.json 和 SILICONFLOW_API_KEY。本地模型实验需显式选择 local-* profile。

## 服务参数

- NOVELTY_HOST / NOVELTY_PORT：监听地址和端口，默认 0.0.0.0:8010。
- NOVELTY_RUNS_ROOT：当前任务工作目录，默认 outputs/web-runs。
- NOVELTY_MAX_UPLOAD_MB：PDF 上传上限，默认 30，需与前端构建配置一致。
- NOVELTY_FRONTEND_DIST：静态构建目录，默认 frontend/dist。

当前接口为 /api/novelty/*。NOVELTY_WEB_RUNS、旧 /api/runs、单线程工作池和旧 PWA 方案已退出。当前任务状态为进程内存；业务文件落盘不等同于 HTTP 任务状态可跨重启恢复。

## 验证

```bash
python -m pytest tests/test_api.py tests/test_workflow.py tests/test_workflow_reviewer.py
cd frontend
pnpm test
pnpm build
```

真实 PaperInput 云端测试记录与验证脚本在 docs/experiments/20261010_web_retirement/。PDF/OCR 实机与浏览器测试属于独立验证范围。
