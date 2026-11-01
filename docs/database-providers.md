# 数据库 Provider 接入

结构化数据库统一通过 `RetrievalSource` 接入。工作流、Researcher、Reader、
Artifact 持久化和 EvidenceCard 构建不包含数据库分支。

## Provider 边界

每个数据库实现一个模块，并提供：

- `QueryAdapter`：把数据库无关 `SearchPlan` 编译为数据库查询；
- `SearchTool`：执行查询并返回统一 `SearchHit`；
- `MetadataTool`：可选，核验标题、DOI 和落地页；
- `DoiLookupTool`：可选，按精确 DOI 返回注册/题录事实和来源覆盖范围；
- `MetadataRetrievalTool`：可选，接收统一查询，返回该来源的独立元数据结果；
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
按来源命名、独立实例化，提供精确 DOI 查询；Crossref 和 DataCite 另支持无 DOI 的题录检索。
上层 `MetadataRetrievalService` 汇总事实，Validator 负责与 Evidence 绑定的 Work 核对。
通过依赖注入启用外部核验；默认 Validator 仍只执行本地检查，不在构造时自动联网。

| Provider 模块 | 类名 | source_id | 已实现能力 |
|---|---|---|---|
| `doi_org.py` | `DoiOrgLookupTool` | `doi_org` | `lookup(doi)` / `retrieve(query)` 查询 Handle；无 DOI 时 unsupported |
| `crossref.py` | `CrossrefMetadataTool` | `crossref` | `lookup(doi)`、`resolve(doi)`；`retrieve(query)` 支持题录检索 |
| `datacite.py` | `DataCiteMetadataTool` | `datacite` | `lookup(doi)`、`resolve(doi)`；`retrieve(query)` 检索公开 Findable 题录 |

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
不创建 HTTP 客户端。这三个来源提供元数据检索能力，不实现 Researcher 使用的主题
`SearchTool`；统一查询使用 `metadata_retrieval_tool`，无需构造 SearchPlan。

### 返回语义与错误

`lookup()` 返回 `DoiLookupResult`：source_id、规范化 requested_doi、status、coverage、
http_status、record。status 只有 found/not_found；失败通过异常表达，不能变成未命中。
`record` 是 `DoiMetadataRecord`，包含 doi、record_kind、可选标题/作者/年份/URL/出版者/
资源类型/摘要/venue 及原始 JSON。可用 `dataclasses.asdict(result)` 序列化。

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

### 统一契约与上层组装（2026-10-27）

调用关系：

```text
Validator → MetadataRetrievalService → doi_org / crossref / datacite
          ← 元数据候选卡 + 各来源原始结果 ← 各来源独立返回
Validator → 对照 Card → Evidence → 本地 Work/SourceRecord，作出一致性判定
```

契约位于 `ports/metadata.py`；`DoiMetadataRecord` 沿用原类型以兼容精确查询，但允许
检索记录的 DOI 缺失。三来源均实现 `retrieve(MetadataQuery)`：

| 对象 | 内容与语义 |
|---|---|
| `MetadataQuery` | 可选 doi、title、authors、year、venue、citation；limit 默认 5，上限 10；至少有 DOI、标题、作者或引文之一 |
| `ProviderMetadataResult` | source_id、query、status、coverage、records、http_status、error_type、truncated；各来源结果独立保留 |
| `MetadataCard` | 一篇候选文献的 metadata、逐字段 field_sources、原始 records、conflicts、urls、missing_fields |
| `MetadataRetrievalResult` | 三来源 providers、候选 cards、执行 status；仅恰有一个候选时 `.card` 非空 |

`retrieve()` 状态区分 found、not_found、unsupported、error。未命中返回空 records；
不支持查询与超时/限流/格式错误分别记录，不伪装成零命中。元数据字符串/数字缺失为 None，
作者缺失为空元组（JSON 中为 []）；不把查询参数、DOI URL 模板或模型推测填成数据库事实。
原有 `lookup()` 保留精确查询的异常接口，以兼容既有调用方。

有 DOI 时各来源先查精确记录；Crossref/DataCite 精确查询明确未找到或返回记录缺 DOI，
且标题、作者和年份齐备时，额外执行一次有界题录查询，逐次查询结果保存在 attempts。
无 DOI 时，Crossref 使用 query.bibliographic、
query.author、query.container-title 及出版日期范围；DataCite 使用标题/作者/出版年的
字段查询；DOI.org 返回 unsupported，不发请求。每个支持的来源只取有界首页，不自动翻页、
递归补查或重试；上述精确查询后的题录查询是有界回退，更多候选用 truncated 标明。
排序靠前不代表真实性已确定。

组装规则：同 DOI 且无字段冲突的记录可以合并；缺少共同 DOI 的记录须标题、完整作者和年份
均一致且无冲突才合并，不同 DOI 不按题名合并。字段冲突时保留独立候选，各候选记录全部冲突值
及来源。一个来源为空、另一个有值时补齐；相同字段一致时保留全部来源。不同落地 URL 全部
保留在 urls，多值时不猜测规范 URL。
“完整元数据卡”指结构、结果与溯源完整，不承诺所有字段非空。顶层 complete 仅表示查询执行
完成，不代表字段完整、来源穷尽或真实性成立。

```python
from novelty_agent_framework.ports import MetadataQuery
from novelty_agent_framework.tools.metadata_retrieval import build_metadata_retrieval_service

with build_metadata_retrieval_service() as metadata:
    result = metadata.retrieve(MetadataQuery(title="Deep learning", year=2015))
    for candidate in result.cards:
        print(candidate.metadata, candidate.missing_fields, candidate.conflicts)
```

来源工厂默认在显式调用时创建三个工具；可传入按 source_id 键控的配置字典，参数与前述
registry 一致。服务关闭其创建的工具；直接注入工具列表时由调用方管理生命周期。
`aretrieve()` 把同步 I/O 放到工作线程，并传递运行时预算上下文。

### Validator 的真实性核对

```python
from novelty_agent_framework.agents.evidence_validator import DefaultEvidenceValidator
from novelty_agent_framework.tools.metadata_retrieval import build_metadata_retrieval_service

with build_metadata_retrieval_service() as metadata:
    validator = DefaultEvidenceValidator(metadata_retrieval=metadata, metadata_query_limit=32)
    # 将 validator 注入 NoveltyWorkflowServices.validator；服务需覆盖整个工作流生命周期。
    # 独立调用 validator.validate(cards, tasks=..., evidence=..., novelty_points=...,
    #                           paper_id=..., reference_store=...) 也使用相同核验路径。
```

先执行本地合法性检查；文件、引文、位置或已有元数据不一致时不联网。仅本地 DOI/URL
信息缺失可继续请求外部事实。来源必须通过引文和标题唯一绑定 Work；查找其实际落盘的
Work/SourceRecord 中一致的作者与年份，不能相信 Card 自行补出的信息。

当前确定性规则：有共同 DOI 时须 DOI 和标题一致；无共同 DOI 时须标题、完整作者序列及年份均
存在且一致，同时对双方提供的页码、页数和 venue 进行比较。标题/作者规范化空白和大小写；
候选之间采用 OR，至少一个匹配即通过，但其他候选的字段冲突或不一致事件仍保留。只有
Handle 时不会通过。该规则可能将姓名写法、出版版本或日期差异留待进一步处理。
它基于返回的有界候选集，不声称搜索已穷尽全部数据库。

查询执行状态与比对结果分开记录。比对失败进入统一错误码系统，明确区分已检索无匹配、
字段缺失、候选歧义、来源冲突、身份不一致和请求故障；不使用笼统的“未核验”标签。
具体错误码统一维护在[集中目录](error-and-supplement-codes.md#validator-元数据核对2026-10-27)，
不据检索无匹配断言文献虚构。已确认一致的外部
标识符仅补足本次校验的本地空缺，不覆盖已有标识符，不改写 Work、Evidence 或文件。
请求完成后重新检查文件和原文；元数据一致不能绕过本地溯源检查。

每次 validate 对相同查询复用结果，默认最多 32 个不同查询；没有跨调用缓存。查询预算
耗尽记录具体预算错误及查询未执行状态。工作流优先调用 avalidate，避免阻塞异步循环。每张 Card 的核对状态、
来源、匹配候选及全部返回结果进入 ValidationResult.integrity.metadata_authenticity，
通过现有 evidence_integrity 状态保留；具体 FailureEvent 进入 ValidationResult.failure_events、
工作流错误列表和 recovery 审计，启用 Runtime 时进入 corrections 检测记录；未通过者
进入拒卡通道，不流入 Reviewer。单来源失败且其余来源足以核对时，仍保留该来源错误事件。
此处验证文献身份一致性；论文内容、引文语义和新颖性仍由原有相应环节负责。

正常工作流按运行创建服务并默认启用外部核验；后端配置 `workflow.metadata_verification_enabled`
可设为 false。初始化失败降级本地模式并记录异常类型，配置关闭和初始化失败分别标记；
服务启动后的单次查询故障只影响相关卡。调用方自行注入的 Validator／服务由调用方管理。

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
精确 DOI 工具阶段的相关离线回归共 **208 项通过**，包含新增 DOI Provider、共享 HTTP、arXiv、Springer、
ScienceDirect、IEEE Xplore、结构化检索和数据库搜索。异步回归在正常本地执行环境运行，
避免沙箱内线程唤醒受限；测试仍使用模拟 HTTP，不发送真实 API 请求。

2026-10-27 在当前大陆校园网节点直接调用新 Provider，四个正例和三个随机负例全部通过。
本地记录为 `outputs/reference_metadata_probe/provider-tools-smoke-20261027T060804Z.json`。
这是新 Provider 的功能验证；之前的一小时探针结果独立保留，没有冒充 Provider 长时运行。

统一契约与 Validator 阶段的最终相关离线回归 **354 passed**。新增测试在
`tests/test_metadata_retrieval.py`、`tests/test_metadata_authenticity.py`，覆盖来源独立返回、
无 DOI 检索、空值、冲突、不同文献不拼接、错误隔离、请求去重/额度、Work/SourceRecord
核对、subject_reference 命名空间、原始清单不变、查询期间文件变化和工作流异步接线。
该数量包含既有 Provider、共享 HTTP、结构化检索及 Validator/门控回归，不是新增测试数。

2026-10-27 统一服务的真实直连验证：精确 DOI `10.1038/nature14539` 汇总出一张候选卡；
无 DOI 的 DataCite 标题+年份查询找到 `10.14454/qdd3-ps68`；无 DOI 的 Crossref
标题+完整作者+年份查询找到 `10.1038/nature14539`。三者通过确定性元数据比较。
仅以短标题“Deep learning”+2015 搜索、每源限 3 条时，目标不在返回集内，未通过比对；当前实现对此记录具体的无匹配错误码（原始测试产物保留当时状态字段）；
不把成功响应等同于验证通过。已将 Crossref 的年份改为出版日期过滤条件，避免混作关键词。
记录分别位于 `outputs/reference_metadata_probe/metadata-service-smoke-20261027T070942Z.json`
和 `outputs/reference_metadata_probe/metadata-author-search-smoke.json`；更早的搜索诊断也保留。
真实验证仅涵盖公共元数据服务与比较规则；带实际 Evidence 文件的工作流接线使用离线夹具验证，
没有重跑真实模型完整工作流。

官方接口依据：
[DOI.org Handle API](https://www.doi.org/the-identifier/resources/factsheets/doi-resolution-documentation)、
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)、
[DataCite 单 DOI](https://support.datacite.org/docs/api-get-doi)、
[DataCite 公共 API 范围](https://support.datacite.org/docs/rest-api)、
[HTTPX Transport](https://www.python-httpx.org/advanced/transports/)。

标题检索接口依据：[Crossref queries/filters](https://github.com/CrossRef/rest-api-doc#queries)、[DataCite queries](https://support.datacite.org/docs/queries)。
