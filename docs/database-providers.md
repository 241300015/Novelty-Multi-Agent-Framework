# 数据库 Provider 接入

结构化数据库统一通过 `RetrievalSource` 接入。工作流、Researcher、Reader、
Artifact 持久化和 EvidenceCard 构建不包含数据库分支。

## Provider 边界

每个数据库实现一个模块，并提供：

- `QueryAdapter`：把数据库无关 `SearchPlan` 编译为数据库查询；
- `SearchTool`：执行查询并返回统一 `SearchHit`；
- `MetadataTool`：可选，核验标题、DOI 和落地页；
- `DoiLookupTool`：可选，按精确 DOI 返回注册/题录事实和来源覆盖范围；
- `FullTextTool`：可选，返回已获授权的可读文本；
- `build_<provider>_source(config)`：组装同一 `source_id` 的能力包。

共享 HTTP 能力位于 `providers/common.py`，负责从环境变量读取密钥、限流和
有限重试。查询语法、认证请求头和响应解析必须保留在具体 Provider 中。

能力按来源提供。只有 DOI 查询能力的来源可以省略 `QueryAdapter` 和 `SearchTool`；
它仍通过同一个 `RetrievalSource` 注册表构建，但不能作为主题搜索来源传给
`StructuredSourceRetrievalTool`。后者仍要求 query_adapter 和 search_tool 同时存在。

## 新增数据库

1. 在 `providers/` 新建模块，例如 `ieee_xplore.py`；
2. 实现上述能力并统一映射为 `SearchHit` / `FullText`；
3. 在 `database_search/factory.py::build_source_registry` 延迟注册 builder；
4. 在 `researcher.example.json` 的 `tools.database_search.providers` 添加默认禁用配置；
5. 在 `backend/.env.example` 只声明环境变量名，不写真实密钥；
6. 使用 `httpx.MockTransport` 覆盖查询、分页、权限、限流、摘要和全文降级测试。

## ScienceDirect

已提供首个 HTTP Provider 骨架：Search V2 返回候选 PII，Provider 对有限数量的
候选调用 Article Retrieval `META_ABS` 补齐摘要，并通过 `FULL` 视图尝试取得全文。
真实密钥只从 `ELSEVIER_API_KEY` 读取；机构令牌为可选的
`ELSEVIER_INST_TOKEN`。全文无权访问时返回 `None`，由通用检索链保留摘要，绝不把
摘要标记为全文。

## Springer Nature

`springer` Provider 使用 Meta API `meta/v2/json` 检索元数据和摘要。全文模式：

- `disabled`：不调用全文接口；
- `openaccess`（默认）：仅对 Meta API 明确标记为开放获取的候选调用
  Open Access JATS 接口；
- `tdm`：调用 `xmldata/jats`，必须配置 `SPRINGER_NATURE_TDM_API_METRIC`，且必须
  已取得 Springer Nature 的专项 TDM 授权。

元数据检索 Key 从 `SPRINGER_NATURE_META_API_KEY` 读取，Open Access JATS Key
从 `SPRINGER_NATURE_OPEN_ACCESS_API_KEY` 读取，两者不混用。JATS 会被解析为纯文本和章节映射；
响应 URL 中的 Key 不会写入 Artifact provenance 或错误信息。
生产或本地部署通过 `NOVELTY_SPRINGER_ENABLED=true` 启用；没有本地凭据的
开发/CI 环境保持默认关闭。

## IEEE Xplore

`ieee_xplore` Provider 使用 Metadata Search API 检索题录和摘要，并以 IEEE
`article_number` 作为全文请求标识。`full_text_mode` 支持：

- `disabled`：只保存题录和摘要；
- `openaccess`（默认）：只调用 IEEE Open Access full-text endpoint，订阅内容自动
  降级到摘要。

API Key 从 `IEEE_XPLORE_API_KEY` 读取。IEEE 的付费全文 API 需要另行签约、客户凭据
和短期 token 流程，本 Provider 不会仅凭普通 API Key 尝试下载付费全文；取得正式
接口参数后应另行扩展，而不是复用开放全文模式。

Springer Nature 和 IEEE Xplore 复用相同 HTTP 基础设施，但各自保留查询编译、响应
映射和授权判断，不复用 ScienceDirect 的 API 假设。

## DOI.org、Crossref、DataCite

三个工具与 `arxiv.py`、`springer.py` 同处 `tools/database_search/providers/`，
按来源命名、独立实例化，承担一个明确的动作：按 DOI 读取该服务的记录。
本阶段没有接入 Validator 决策、Researcher 搜索、自动回退或 LLM 调用，也不默认启用。

| Provider 模块 | 类名 | source_id | 已实现能力 |
|---|---|---|---|
| `doi_org.py` | `DoiOrgLookupTool` | `doi_org` | `lookup(doi)` 查询 Handle 注册信息与 URL |
| `crossref.py` | `CrossrefMetadataTool` | `crossref` | `lookup(doi)` 查询题录；`resolve(doi)` 兼容 MetadataTool |
| `datacite.py` | `DataCiteMetadataTool` | `datacite` | `lookup(doi)` 查询公开 Findable 资源题录；`resolve(doi)` 兼容 MetadataTool |

统一的底层接口及返回类型在 `ports/doi.py`，通过 `novelty_agent_framework.ports` 导出：

```python
from novelty_agent_framework.tools.database_search.providers.crossref import (
    CrossrefMetadataTool,
)

with CrossrefMetadataTool() as crossref:
    result = crossref.lookup("10.1038/nature14539")
    if result.status == "found":
        print(result.record.title, result.record.authors, result.record.year)
    else:
        print("该来源未返回记录", result.coverage)
```

`DoiOrgLookupTool`、`DataCiteMetadataTool` 以相同方式调用 `lookup()`。
也可以沿用现有的来源工厂，按名字获取底层能力：

```python
from novelty_agent_framework.tools.database_search import build_source_registry

source = build_source_registry().build("datacite", {
    "enabled": True,
    "timeout_seconds": 10,
    "ip_family": "ipv4",
    "min_interval_seconds": 0.5,
    "max_retries": 0,
})
assert source.doi_lookup_tool is not None
with source.doi_lookup_tool as datacite:
    result = datacite.lookup("https://doi.org/10.14454/qdd3-ps68")
```

上述 registry 是 arXiv、Springer 使用的同一个注册表。`enabled=false` 的 builder
不创建 HTTP 客户端。不要把这三个仅支持精确 DOI 查询的来源配置为 Researcher 的主题
搜索来源；上层若要使用，应显式调用 doi_lookup_tool，而非伪造 SearchPlan。

### 返回语义与错误

`lookup()` 返回 `DoiLookupResult`：source_id、规范化 requested_doi、status、coverage、
http_status、record。status 只有 found/not_found；失败通过异常表达，不能变成未命中。
`record` 是 `DoiMetadataRecord`，包含 doi、record_kind、可选标题/作者/年份/URL/出版者/
资源类型/摘要及原始 JSON。可用 `dataclasses.asdict(result)` 序列化。

- DOI.org：coverage=handle_registry、record_kind=handle。Handle 不提供论文题录，
  title/year/publisher 保持 None；不实现要求标题的 MetadataTool.resolve。
  responseCode=100 与 404 才是明确未找到；responseCode=200 表示 Handle 存在但无值。
- Crossref：coverage=crossref_metadata、record_kind=bibliographic。未命中仅表示该
  来源没有返回题录，不否定其他注册机构的 DOI。
- DataCite：coverage=datacite_findable、record_kind=bibliographic。匿名公共 API
  不覆盖所有注册状态，404 不能证明 DOI 未注册；数据集、软件、文档等也不能都当作期刊论文。
- 输入可为裸 DOI、`doi:` 前缀或 DOI.org/dx.doi.org URL。DOI 统一转为小写，不删末尾标点；
  URL 的 DOI 后缀含 `?`/`#` 时须先编码。请求中整个 DOI 被编码，防止后缀变成查询参数或路径跳转。
- HTTP 200 仍需核验 JSON 结构、目标 DOI 一致性；DataCite 同时核验 data.id 和 attributes.doi。
  HTML/验证码、格式错误、身份不匹配抛 DoiResponseError（ValueError 子类）。
- 403、429、5xx、重定向及无法识别的 404 不返回 not_found。HTTP/网络失败使用现有
  ProviderRequestError，保留 status_code 或 transport_error_type。
- Crossref/DataCite 的 `resolve()` 返回已有 EvidenceSource 类型；仅明确未命中返回 None。
  若记录存在但缺少标题，lookup() 仍保留记录，resolve() 抛 DoiResponseError，避免编造标题。

三个工具均不做 DOI 真伪裁决、论文内容验证、跨来源回退或全文下载。

### 连接与生命周期

默认使用 IPv4、验证 TLS、忽略代理环境变量，不跟随重定向、不自动重试。只影响工具自身
HTTP 客户端，保持 Clash/系统代理不变。`ip_family` 可显式选择 auto/ipv4/ipv6；
timeout 是 HTTPX 的各阶段 I/O 超时，不是整个请求的总计时上限。单个工具实例复用连接池，
默认请求间隔至少 0.5 秒；限流锁仅在该实例内共享，多实例调用方需自行协调总速率。

支持传入 `client=httpx.Client(...)` 以测试或提供自定义连接策略；注入客户端的代理、TLS
和地址族由调用方负责，工具仍强制不跟随重定向并设置请求超时。默认客户端由工具的
close()/上下文管理器关闭；注入客户端由调用方关闭。没有成功/失败缓存，后续重复 lookup
会重新请求，不会把暂时的失败固定为缺失结果。

传输复用现有 ResilientHttpClient/HttpRequestPolicy；如显式配置重试，沿用其有界的
429/5xx 重试规则。每次实际 HTTP 请求参与已有运行时预算，按 doi_org/crossref/datacite
分别标记；现有认证数据库的默认预算标签不变。

### 验证

离线测试使用 httpx.MockTransport，覆盖精确 URL/编码、题录映射、Handle 语义、合法/非法
404、403/429/5xx、重定向、错误 JSON、身份不一致、超时、预算、生命周期、来源工厂及
默认连接策略：`python -m pytest -q tests/test_doi_metadata_providers.py`。
最终相关离线回归共 **208 项通过**，包含新增 DOI Provider、共享 HTTP、arXiv、Springer、
ScienceDirect、IEEE Xplore、结构化检索和数据库搜索。异步回归在正常本地执行环境运行，
避免沙箱内线程唤醒受限；测试仍使用模拟 HTTP，不发送真实 API 请求。

2026-10-27 在当前大陆校园网节点直接调用新 Provider，四个正例和三个随机负例全部通过。
本地记录为 `outputs/reference_metadata_probe/provider-tools-smoke-20261027T060804Z.json`。
这是新 Provider 的功能验证；之前的一小时探针结果独立保留，没有冒充 Provider 长时运行。

官方接口依据：
[DOI.org Handle API](https://www.doi.org/the-identifier/resources/factsheets/doi-resolution-documentation)、
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)、
[DataCite 单 DOI](https://support.datacite.org/docs/api-get-doi)、
[DataCite 公共 API 范围](https://support.datacite.org/docs/rest-api)、
[HTTPX Transport](https://www.python-httpx.org/advanced/transports/)。
