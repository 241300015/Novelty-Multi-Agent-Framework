# env 目录说明

该目录用于约定团队统一的模型调用方式，避免不同开发者在不同 Agent 中各自拼接 API 请求。

实际可导入代码位于 `backend/env/`：

- `model_client.py`：统一模型配置、消息格式、响应格式和 OpenAI-compatible 调用客户端；
- `model_client.py` 中的 `ModelProfile` / `ModelRegistry`：按业务别名注册多模型，`client_for(alias)` 返回独立客户端；
- `prompt_library.py`：加载并渲染 `prompts/` 下的版本化提示词模板（front matter + 变量占位符）；
- `__init__.py`：导出公共模型调用对象，便于 Agent 统一引用。

新增真实 Agent 时，应优先通过 `build_model_client()` 获取模型客户端，而不是在 Agent 内部直接读取 API Key 或手写 HTTP 请求。

多模型场景建议直接使用 `ModelRegistry`：启动时从配置构建 profile 注册表，
按角色（coordinator / research / validator）取用不同模型。厂商私有参数
（如 `enable_thinking`、`reasoning_effort`）通过 `ModelCallOptions.extra_body`
透传，发送前按 profile 的 `supported_params` 白名单过滤。

开发环境可在 `backend/.env` 中写入 `SILICONFLOW_API_KEY=...` 等密钥，
`model_client.py` 导入时会通过 python-dotenv 自动加载（真实环境变量优先）。
`.env` 已被 `.gitignore` 排除，不会进入 git；生产部署仍使用环境变量。

首次配置时复制 `backend/.env.example` 为 `backend/.env`，再只在 `.env` 中
填写真实的 `SILICONFLOW_API_KEY`。不要直接修改 `.env.example` 写入密钥；该文件
是需要提交到 Git 的空值模板。可在不输出密钥内容的前提下验证加载结果：

```bash
conda run -n Novelty python -c "from novelty_agent_framework.config import load_config, build_model_registry; registry = build_model_registry(load_config()); print(all(profile.api_key for profile in registry._profiles.values()))"
```
