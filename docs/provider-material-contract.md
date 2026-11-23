# Provider 原始材料格式盘点与共享契约 v0

核对日期：2026-11-15（Asia/Shanghai）。代码基线：`fix/arxiv-provider`，`7ff63dd061db0470cd15462e85d20ae3af40c180`。

状态：格式范围与交接原则的调研基线文档；arXiv 全文已按 v1 接入原始文件保存；2026-11-16 起获取流程不再调用 Reader，摘要仅保存到 JSON。下表记录改动前的实现，当前状态见 v1。用于 Provider 与 Reader 分支共同开发，不能理解为所有格式已经可用。本文覆盖当前注册的全部来源及 ChinaXiv 占位模块，不声称穷尽所有出版社和附件格式。

字段级接口、请求/结果及定位规则已确定，见 [Provider—Reader 共享接口 v1](provider-reader-interface-v1.md)。双方实现以 v1 为准；本文保留格式依据和设计背景。

## 1. 当前代码实际请求的格式

以下“响应格式”指 HTTP 返回体进入解析器前的格式，不是当前最终落盘的 `.txt`。已实现请求路径不代表本次已用真实凭据验证。此次核对为代码审阅和官方文档检索，未调用收费全文接口。

| Provider | 检索 / 元数据响应 | 当前全文请求 | 当前交给存储层的内容 | 代码依据 |
| --- | --- | --- | --- | --- |
| arXiv `web` | 搜索页和 `/abs/{id}` 的 HTML；解析单篇题录和摘要 | `/html/{id}` HTML，失败后 `/pdf/{id}` PDF | HTML 去标签或 PDF 经 PyMuPDF 提取后的纯文本 | [arxiv_web.py](../backend/src/novelty_agent_framework/tools/database_search/providers/arxiv_web.py)、[arxiv.py](../backend/src/novelty_agent_framework/tools/database_search/providers/arxiv.py) |
| arXiv `api` | Atom XML，`entry/summary` 是摘要 | 与 web 使用同一全文工具，HTML → PDF | 纯文本；Atom 不是论文全文 | [arxiv.py](../backend/src/novelty_agent_framework/tools/database_search/providers/arxiv.py) |
| ScienceDirect | Search V2 PUT JSON；Article Retrieval `META_ABS` JSON | Article Retrieval `view=FULL`，请求 `Accept: text/plain` | 响应字符串，可按字符上限截断；不是我们从 PDF 提取出的文本 | [sciencedirect.py](../backend/src/novelty_agent_framework/tools/database_search/providers/sciencedirect.py) |
| Springer Nature | `/meta/v2/json` JSON，含摘要及可能的 HTML/PDF 链接 | `/openaccess/jats` 或授权 TDM `/xmldata/jats`，请求 XML | 解析为纯文本和章节映射，再保存文字 | [springer.py](../backend/src/novelty_agent_framework/tools/database_search/providers/springer.py) |
| IEEE Xplore | Metadata Search 请求 JSON，含摘要及可能的 HTML/PDF 链接 | OA `/search/document/{id}/fulltext?format=xml`，请求 XML | XML 解析后的纯文本和章节映射 | [ieee_xplore.py](../backend/src/novelty_agent_framework/tools/database_search/providers/ieee_xplore.py) |
| DOI.org | `/api/handles/{doi}` Handle JSON | 无全文能力 | Handle 注册事实；不是完整论文题录 | [doi_org.py](../backend/src/novelty_agent_framework/tools/database_search/providers/doi_org.py) |
| Crossref | `/works`、`/works/{doi}` JSON | 无全文下载；响应中的链接仍只是链接 | 元数据记录及原始 JSON 对象，不走全文 Artifact 路径 | [crossref.py](../backend/src/novelty_agent_framework/tools/database_search/providers/crossref.py) |
| DataCite | `/dois`、`/dois/{doi}` JSON:API | 无全文下载 | 元数据记录及原始 JSON 对象；资源可能是数据集或软件 | [datacite.py](../backend/src/novelty_agent_framework/tools/database_search/providers/datacite.py) |
| null_catalog | 不联网，返回空候选 | 无 | 无文件 | [null_catalog.py](../backend/src/novelty_agent_framework/tools/database_search/providers/null_catalog.py) |
| ChinaXiv | 占位，未注册为可用来源 | 未实现、未确认 | 无已支持格式 | [chinaxiv.py](../backend/src/novelty_agent_framework/tools/database_search/providers/chinaxiv.py) |

完整注册列表见 [factory.py](../backend/src/novelty_agent_framework/tools/database_search/factory.py)。ChinaXiv 的历史探针描述不作为今天接口可用性的证据。

当前摘要从 `SearchHit.abstract` 保存为 `role=abstract`；全文保存为 `role=extracted_text`。两者通过同一个 `_try_save_artifact` 写入 `.txt`、声明 `text/plain`。见 [structured_retrieval.py](../backend/src/novelty_agent_framework/tools/database_search/structured_retrieval.py)。

### 原接口不能直接承载新语义

- [FullText](../backend/src/novelty_agent_framework/ports/interfaces.py) 的核心字段是 `text: str`，不适合无损承载 PDF 字节。
- Springer/IEEE 的 `FullText.text` 已是提取后的纯文本，`media_type` 却仍沿用 XML 响应头。它描述的是输入格式，不能直接拿来决定输出文件后缀或 Reader 解析器。
- 原文可能被 `max_chars` 截断，不能把现有文本重新命名为 HTML/XML 当作原始文件。
- [ReferenceStore.write_document](../backend/src/novelty_agent_framework/persistence.py) 当前走 UTF-8 字符串写入；原始 PDF 需要独立的字节写入能力。

## 2. 官方提供、但当前未全部接入的格式

arXiv API 的返回是 Atom XML 元数据，包含摘要；全文是另外获取的资源。[官方 API 手册](https://info.arxiv.org/help/api/user-manual.html)

arXiv 官方列出 PDF、HTML，以及取决于单篇提交的 PostScript、DVI、源文件。TeX 源文件下载可以是 gzip 压缩的单文件，或含图等资源的 tar.gz 包；这些均未接入当前 Provider。[官方阅读与下载说明](https://info.arxiv.org/help/view.html)

Crossref REST 返回 JSON 元数据；全文链接指向其他提供方，并不表示 Crossref 保存或返回论文全文。[Crossref 全文访问说明](https://www.crossref.org/documentation/retrieve-metadata/rest-api/text-and-data-mining-for-researchers/)

DataCite REST 返回遵循 JSON:API 的元数据；当前接入没有下载链接指向的资源。[DataCite API](https://support.datacite.org/docs/api)。DOI.org Handle REST 返回 JSON，和访问 DOI 后重定向到出版社网页是两条不同路径。[DOI Resolution Documentation](https://www.doi.org/the-identifier/resources/factsheets/doi-resolution-documentation)

| 来源 | 官方确认但当前未全部使用的格式 | 接入决策 |
| --- | --- | --- |
| Elsevier Article Retrieval | 原生 XML（Elsevier XOCS/DTD）、纯文本、权限相关 PDF；JSON 是不能完整表达原生全文的转换；另有首页 PNG 和 RDF/XML | 正文优先保留原生格式；不能把 PNG 预览或不完整 JSON 当全文。该 API 已停止 HTML/ePub/MOBI。见 [Article Retrieval WADL](https://dev.elsevier.com/documentation/FullTextRetrievalAPI.wadl) |
| Springer | 元数据 JSON/PAM XML 等；正文 JATS/BITS XML（文章与书籍章节）；HTML/PDF 可作为元数据外链 | `/openaccess/json` 是元数据；正文用 `/openaccess/jats`；TDM 另行授权。见 [Open Access API](https://dev.springernature.com/docs/api-endpoints/open-access/)、[格式说明](https://dev.springernature.com/docs/quick-start/restful-ops-and-response-formats/) |
| IEEE | 元数据及开放全文均支持 JSON/XML；付费全文另需授权；HTML/PDF 是返回字段中的外链候选 | 当前只接全文 XML；XML 方言未被官方资料确认为 JATS。见 [交互文档](https://developer.ieee.org/io-docs)、[SDK](https://developer.ieee.org/Python3_Software_Development_Kit) |

逐接口官方核对见文末；需以方法、路径、view、format 和访问权限一起判断，不能只按数据库名字选择文件后缀。

### 按上游能力选择首选格式（2026-11-15 补充）

“最支持”需区分两个目标：原生结构及信息保留，与文献覆盖范围。下表是依据官方文档作出的工程建议，不受当前实现限制；没有账号级覆盖率对比，也不能把 API 默认格式等同于内容最完整。

| 来源 | 官方事实 | 本项目获取优先级建议 |
| --- | --- | --- |
| ScienceDirect / Elsevier | Article Retrieval 明确全文原生格式是 XML，默认 Accept 为 text/xml；JSON 无法准确表示全部复杂标记 | 结构化全文首选 Elsevier XOCS/DTD XML。PDF 作为版面材料或可用性回退，需相应权限；不是先取 text/plain。依据：[Article Retrieval](https://dev.elsevier.com/documentation/FullTextRetrievalAPI.wadl) |
| Springer Nature | 全文 OA/TDM 的 jats 路径提供 JATS/BITS XML；JSON OA 路径是元数据 | 全文首选 JATS/BITS XML；元数据使用 JSON。PDF/HTML 外链需要独立获取验证。依据：[格式说明](https://dev.springernature.com/docs/quick-start/restful-ops-and-response-formats/)、[OA 接口](https://dev.springernature.com/docs/api-endpoints/open-access/) |
| IEEE Xplore | 全文和元数据 API 均支持 XML/JSON，未指定时默认 JSON；未确认 XML 方言，也没有证据表明 XML 比 JSON 更完整 | XML 可以作为统一结构化全文处理的候选首选，但属工程选择，不是官方质量排序；用同一篇获授权文章的两种响应比较后再固定。依据：[I/O 文档](https://developer.ieee.org/io-docs) |
| arXiv | 大多数文章提供 PDF；HTML/源文件取决于具体文章；API Atom XML 是元数据 | 结构化阅读使用 HTML 优先、PDF 回退；覆盖优先时以 PDF 为主。没有从已核实官方接口取得通用论文全文 XML 的依据。依据：[阅读格式](https://info.arxiv.org/help/view.html)、[API 手册](https://info.arxiv.org/help/api/user-manual.html) |
| Crossref | REST 提供 JSON 元数据，全文由链接指向的提供方交付 | 题录检索首选 REST JSON；若要原始登记 XML 属于不同用途，不将其充作论文全文。依据：[全文访问](https://www.crossref.org/documentation/retrieve-metadata/rest-api/text-and-data-mining-for-researchers/) |
| DataCite | REST 返回 JSON:API 元数据；另有 XML 等元数据导出 | 题录检索首选 REST JSON；DataCite XML 不等于全文。依据：[REST API](https://support.datacite.org/docs/api) |
| DOI.org | Handle REST 返回 JSON；普通 DOI URL 重定向到资源地址 | Handle 查询用 JSON；题录由注册机构接口取得，全文由最终提供方决定。依据：[DOI 解析文档](https://www.doi.org/the-identifier/resources/factsheets/doi-resolution-documentation) |
| ChinaXiv | 官方站点有 PDF 阅读/下载材料；提交支持 Word/WPS/LaTeX 不等于这些均可对外下载 | PDF 作为待接入候选；本轮没有确认稳定的全文 API 或 XML 全文服务，不声明“最佳官方 API 格式”。依据：[官方帮助](https://www.chinaxiv.org/user/help.htm)、[站内 PDF 样本](https://chinaxiv.org/user/view.htm?filetype=pdf&uuid=0da708d5949343b192993f7b106fa916) |

因此全文的重点可收敛为 XML、HTML、PDF，但共享存储契约仍保留 JSON 和纯文本能力。XML 内需分别识别 Elsevier、JATS、BITS 和尚待核对的 IEEE 结构；使用同一后缀不能保证同一个正文解析器适用。此处仅更新获取策略文档，未改变代码或默认配置。

## 3. v0 格式范围

当前接入所需的基本格式族为 **HTML、PDF、XML、JSON、纯文本**。物理格式与语义用途分开表达。

| 格式族 | 常见媒体类型 / 建议后缀 | 本项目需要区分的内容 |
| --- | --- | --- |
| HTML | `text/html` → `.html`；XHTML 若实际取得则保留其媒体类型 | 搜索结果页、摘要页、正文页是不同用途；页面还可能是错误或登录页 |
| PDF | `application/pdf` → `.pdf` | 正文二进制；链接存在不等于已经取得文件 |
| XML | `application/xml`、`text/xml`、带 `+xml` 的具体类型 → `.xml` | Atom 元数据、JATS/BITS 正文、IEEE XML、Elsevier XML 等需区分 schema/profile；不得统一假定 JATS |
| JSON | `application/json`、`application/vnd.api+json` 等 → `.json` | 搜索结果、元数据、API 正文结构都可能是 JSON，不能用格式推断 role |
| 纯文本 | `text/plain` → `.txt` | 上游直接提供的正文，与本地从其他格式提取出的文字要区分 |

`format_family` 是解析分派用的粗分类，不应抹掉精确的媒体类型。`schema_hint` 是可扩展的可选提示，具体 XML 方言未经确认就留空或标明 provider-specific，不编造标准名称。HTTP 声明类型和验证后的文件类型应分别保存，不能只信扩展名或 `Accept` 请求头。

以下保留为扩展范围，**不在本轮承诺 Reader 支持**：TeX/LaTeX、gzip/tar/zip 源文件包、PostScript/DVI、首页 PNG 预览、BibTeX/RIS/Turtle/JSONP 等额外元数据序列化，以及未来新增端点带来的图片、表格、数据、视频等附件（逐项官方依据见文末）。打包格式是容器，不是论文语义类型；HTML 中引用图片也不等于该图片已被下载。原始单个 HTML 文件不承诺离线图像、样式与脚本齐全。格式无法识别时明确 unsupported，不伪装成文本。

## 4. Provider → 存储 → Reader 的共享契约原则

字段命名与 Reader 定位结构现已在 [共享接口 v1](provider-reader-interface-v1.md) 中明确；本节保留设计依据，不在两个分支分别定义不兼容的类型。

| 信息组 | 必须表达的内容 |
| --- | --- |
| 原始内容 | 响应体字节或已保存文件句柄；不能只接受 `text: str`。所谓原始是 HTTP 客户端完成传输解码后的响应体，尚未解码字符、去标签、重排或裁剪，不是网络抓包字节 |
| 格式 | 服务端声明的 Content-Type、验证后的 media_type/format_family、可选字符编码及 schema_hint；未知保持未知 |
| 用途 | 区分搜索响应、元数据、摘要、全文、补充材料。原始/派生另作标记，不靠后缀表达 |
| 身份 | provider、外部标识、实际观测的版本；成功归属单篇后关联 Work/SourceRecord。不能把查询目标当作响应已确认的身份 |
| 来源 | 脱敏后的请求 URL、最终响应 URL、获取时间、HTTP 状态；不得持久化凭据或含 key 的查询参数 |
| 完整性 | 文件哈希、字节数、响应是否完整接收、正文覆盖是否已确认。HTTP 200/无截断不能单独证明论文全文完整 |
| 派生关系 | 从哪份原材料提取，以及所用记录选择器、转换器版本；派生内容使用自己的哈希与定位 |

### 处理多篇响应

检索 HTML、Atom feed、JSON 搜索响应和部分全文 XML 包装可能包含多条记录。当前 Artifact 要求单个 `work_id`，不能将整个批响应强行归属一篇论文，也不能给每篇复制一份后声称都是单篇全文。

建议：批响应保留在请求级记录；单篇材料通过 record selector（例如 JSON Pointer、XML ID/路径或稳定 HTML 节点标识）关联父响应。若提取并另存单篇片段，应标为派生，并保留父响应哈希。具体 selector 结构采用共享接口 v1 的 RecordSelector；其绑定哈希与解析缓存一起校验。

### 每层负责什么

- **Provider**：协议、认证、限流、重试、格式与身份核验、获取原始材料；仍解析题录以支持搜索，但不把全文强制压成纯文本。
- **存储层**：按实际格式保存字节并登记 Manifest，保留原始与派生关系；失败响应不能登记为成功全文。
- **Reader**：按明确支持的格式/profile 读取材料。原始保存不代表一定直接把 HTML/XML 源码给模型；是否生成正文视图属于 Reader 实现。
- **证据定位**：位置必须绑定实际读取的 representation 及其哈希。HTML 正文视图字符偏移不等于原 HTML 源码偏移；现有 `ReferenceReadResult` 的字符不变量不能静默改变。[现有读取契约](../backend/src/novelty_agent_framework/schemas/research_tools.py)

### 成功与降级

取得全文、只有摘要、资源不存在、未授权、不支持该格式、请求失败必须能区分；不能全部压成空字符串或“零命中”。格式支持是本地能力，访问权限是上游能力，两者独立。

摘要可以继续是独立的派生材料，用于候选筛选；它需要准确的 role 和父材料溯源。ScienceDirect 直接返回 `text/plain` 时，保存 `.txt` 是正确的原始保存，不应强制转换成 HTML。

## 5. 两个分支的实施顺序与验收依据

1. 一个共享接口提交定义原始字节 / 文件引用、格式与用途、来源、完整性及原始/派生关系；Provider 和 Reader 使用同一提交。
2. Provider 分支改获取和存储，交付固定的原始材料 + Manifest 样本；不直接改另一分支的 Reader 内部实现。
3. Reader 分支声明支持的格式/profile，完成读取与位置关联，保留旧文本材料读取兼容性。
4. 集成分支按固定提交联调；Reader 接通前不把默认全文路径切成其尚不支持的格式。

共享样本至少覆盖：arXiv 正文 HTML/PDF、原生纯文本、Springer JATS、真实 IEEE XML（不能只用手写 JATS-like fixture 证明兼容）、元数据 JSON、包含多个 entry 的 Atom。未来启用 IEEE 全文 JSON 时再加入对应真实样本。还需验证只有摘要的 XML、错误 HTML、媒体类型不匹配、部分响应和版本不一致，均不会冒充有效全文。

已有 Springer/IEEE 单元测试使用人工 XML，能证明本地分支行为，不能证明任意真实 XML 方言均可解析。本轮没有修改运行时代码，也没有重跑真实工作流。

---

# 附录：出版商接口官方来源核对

核对日期：2026-11-15。范围：Elsevier ScienceDirect、Springer Nature、IEEE Xplore；附 arXiv 与 DOI/Crossref/DataCite。通过联网检索并打开官方文档核对；未调用需要密钥、可能收费的内容 API，未下载付费正文，未修改项目源码。下文“支持”指文档契约，不保证当前项目账号权限或每篇文章都有该格式。Springer 部分页面的直接打开仅显示站点外壳，正文来自同一官方页面的搜索索引。

## 可以直接用于共享材料契约的盘点

| 来源 / 操作 | 检索或元数据响应 | 正文 / 文件材料 | 必须保留的区别 |
|---|---|---|---|
| ScienceDirect Search V2 | 原生 PUT：JSON；兼容 GET：JSON、XML、Atom XML | 搜索结果提供全文接口/网页链接，本身不是全文 | 全文检索不等于返回全文；原生 PUT 与兼容 GET 的响应能力不同 [E1] |
| Elsevier Article Retrieval | META / META_ABS / META_ABS_REF 等视图；JSON/XML 可作元数据响应 | Elsevier 原生 XML；简化纯文本；PDF（权限相关）；JSON 只是不完整转换；PNG 为首页预览；RDF/XML 另作结构化表示 | XML 为 Elsevier DTD/XOCS，不是 JATS；自 2017-08-29 停止 HTML/ePub/MOBI；不能把 HTTP 200、JSON 或默认视图视为已得到全文 [E2–E5] |
| Springer Meta / Metadata | JSON；旧 Metadata 文档还明确 PAM XML、JSONP | Meta 是元数据，不是正文 | 不从通用格式枚举推断每个版本端点都支持所有格式 [S1–S3] |
| Springer Open Access | `/openaccess/json`：元数据 | `/openaccess/jats`：可用时的全文 XML（文章 JATS、章节 BITS） | JSON 元数据中的 HTML/PDF 链接不是 JSON 全文，也不等于 API 直接返回 HTML/PDF [S2,S4,S7] |
| Springer Full Text TDM | 单独的授权接口 | `/xmldata/jats`：可用时的 JATS/BITS XML | 需专项协议/权限；旧域名迁移期限已过，当前示例使用 `api.springernature.com` [S5,S6] |
| IEEE Metadata Search / DOI | JSON（默认）、XML | 元数据可含 `pdf_url`、`html_url`，后续取回另算 | 摘要、全文链接、可搜索文档文本都不等于已取回正文 [I1,I2] |
| IEEE OA fulltext | `/api/v1/search/document/{id}/fulltext` 明确 JSON/XML 响应，默认 JSON | API 返回 OA 文章全文；正文内部 schema/标记方言未确认 | 可以记录“IEEE 全文 API 响应 JSON/XML”；不能无证据命名 `jats_xml`，也不能把其 XML/JSON包装与网页 PDF/HTML 混成同一层 [I1,I3] |
| IEEE chargeable fulltext | 官方 SDK 支持原始 XML/JSON 输出，并提供 JSON 请求示例 | 付费全文另需 auth token 与销售/机构协议 | 不能推断普通 Metadata key 或网页订阅已覆盖付费 API；本次不触发请求 [I3,I4] |

## Elsevier：原生格式、视图与权限

- [E1] [ScienceDirect Search API V2 WADL](https://dev.elsevier.com/documentation/ScienceDirectSearchAPI.wadl)：明确搜索返回 relevant result metadata；PUT 是原生推荐接口，Accept 目前仅 JSON；GET 是模拟的兼容层，Accept 列 JSON、Atom、XML。因此[总览表](https://dev.elsevier.com/sd_api_spec.html)的三种格式必须附方法限定，不能全部归给 V2 PUT。
- [E2] [Article Retrieval WADL](https://dev.elsevier.com/documentation/FullTextRetrievalAPI.wadl)：列 `text/xml`、`application/json`、`application/pdf`、`image/png`、`text/plain`、`application/rdf+xml`；声明原生正文是 XML，JSON 无法准确承载完整复杂标记；PNG 是文章第一页；HTML/ePub/MOBI 已停止支持。XML schema 指向 Elsevier `xocs:doc`/`xocs-article.xsd`。这些属于不同能力，RDF/XML 不应自动算可读全文，JSON 也不应算无损结构化全文。
- [E3] [Text Mining 技术说明](https://dev.elsevier.com/tecdoc_text_mining.html)：`text/xml` 获取 Elsevier DTD 的完整结构；`text/plain` 获取去标记的简化正文。显式 `view=FULL` 在无全文权限时返回错误；不指定 FULL 时可能退到摘要。该说明与 WADL 的默认 `META` 标示存在默认值表述差异，适配器应显式指定视图并验证正文存在，不依赖默认。
- [E4] [Article access troubleshooting](https://dev.elsevier.com/tecdoc_article_access.html)：机构/IP、账号和 key 权限共同影响内容访问；key 可允许文本格式而限制 PDF 等格式。因此 PDF 是“接口列出的能力，按账号/资源权限可用”，不是“任何 key 均可下载”。该页仍提 HTML/ePub，属于历史叙述；格式支持以 E2 明确停止声明为准。
- [E5] [Search V2 Views](https://dev.elsevier.com/sd_search_views.html)：OA 标记不授予 Search API 使用资格；请求还需有效 key 和适用 API/ScienceDirect 权限或获批集成。
- [E6] [Object Retrieval WADL](https://dev.elsevier.com/documentation/ObjectRetrievalAPI.wadl)：关联对象可包括 PDF、JPEG/GIF/PNG/SVG/TIFF、XML/MathML、Office/RTF/纯文本、音视频、ZIP/TAR、KML/KMZ 等。带 view 和 XML/JSON/RDF 等支持格式时可能返回对象元数据而非对象二进制。此能力应归“附件/关联对象”，不把所有 MIME 当作论文正文格式。

## Springer：JATS 与 BITS，元数据与外链

- [S1] [Meta API](https://dev.springernature.com/docs/api-endpoints/meta-api/)：`/meta/v1` 与 `/meta/v2` 提供元数据；官方示例为 `/meta/v2/json`。未获得足够端点级证据证明 `/meta/v2/pam` 等全部组合，暂不承诺。
- [S2] [RESTful Operations and Response Formats](https://dev.springernature.com/docs/quick-start/restful-ops-and-response-formats/)：列 PAM XML、JATS/BITS XML、JSON、JSONP；给出 `/metadata/pam` 实例。路径 `jats` 同时用于 JATS 文章与 BITS 章节，因此 Reader 要区分内容方言/根元素，不能把所有返回都当 `<article>`。
- [S3] [API Playground](https://dev.springernature.com/docs/live-documentation/)：Metadata 示例为 `/metadata/json`，输出选项列 JSON/PAM/JSONP；[旧 Metadata API 页面](https://dev.springernature.com/docs/api-endpoints/metadata-api/)将其定义为元数据服务。
- [S4] [Open Access API](https://dev.springernature.com/docs/api-endpoints/open-access/)：明确 `/openaccess/json` 获取元数据，`/openaccess/jats` 获取可用的全文。不能因 OA API 总称“metadata and full-text”就推断 JSON 包含正文。
- [S5] [Full Text API](https://dev.springernature.com/docs/api-endpoints/fulltext-api/)：`/xmldata` 依专项协议提供可用的文章/章节全文，具体请求示例为 `https://api.springernature.com/xmldata/jats?...`，示例含 `<book-part-wrapper>`；文档要求 2026-08-07 前从 `spdi.public.springernature.app` 迁移到 `api.springernature.com`。
- [S6] [Subscription Models](https://dev.springernature.com/subscription/)：Meta/OA 与 TDM 是不同服务/授权范围，TDM 存在订阅内容和定制方案；本次未验证项目 key 是否开通。
- [S7] [Making First API Call](https://dev.springernature.com/docs/quick-start/making-first-api-call/)和[Advanced Query Examples](https://dev.springernature.com/docs/advanced-querying/example-advanced-queries/)的元数据示例含 `url[].format=html/pdf`、`platform=web`。这是网页侧 HTML/PDF 的定位证据，不是 `/meta`/`openaccess` 直接返回二进制 PDF 的证据。每条外链是否可取、是否全文、是否需登录需后续验证。

## IEEE：可确认 JSON/XML 全文响应，未确认正文方言

- [I1] [Interactive Documentation](https://developer.ieee.org/io-docs)：Metadata 与 DOI API 均明确 JSON/XML；OA fulltext 也明确 `format=json|xml`，默认为 JSON。结论不应写成“IEEE JSON 永远只是元数据”，应结合具体操作判断。
- [I2] [Data Fields Returned](https://developer.ieee.org/docs/read/Metadata_API_responses)：`pdf_url` 是全文 PDF 链接，`html_url` 是全文 HTML 链接；`accessType` 标注 OA、Ephemera、Locked 等；全文需按 article number 再请求。字段仅在有值时出现，因此不能要求每篇都有 HTML/PDF 链接。
- [I3] [Python 3 SDK](https://developer.ieee.org/Python3_Software_Development_Kit)：`openAccess()` 返回 OA 文章全文；`dataType()` 支持 JSON/XML；付费示例使用 `setAuthToken()`、`dataType('json')`、`dataFormat('raw')`、`fullTextRequest()`。文档明确付费全文需联系 IEEE 开通。
- [I4] [Currently Available APIs](https://developer.ieee.org/docs)区分 Metadata、OA 和 chargeable fulltext；[Java SDK](https://developer.ieee.org/Java_Software_Development_Kit)说明付费全文需要 auth token，并列 JSON/XML 序列化方法。
- 未确认：公开页面没有足够证据把全文 XML 定义为 JATS，也没有证实 API `format=pdf/html`。目前应保留 `ieee_fulltext_response` 的 JSON/XML 原样字节和媒体类型；PDF/HTML 作为元数据返回的独立 web link 候选。后续用获授权样本检查 JSON 是否包含原生 XML/HTML/正文字符串、以及全文 XML 的 namespace/root/schema，再决定 Reader 适配器。

## 附：arXiv、DOI.org、Crossref、DataCite

| 来源 | 已核实的原始格式 | 限定和官方证据 |
|---|---|---|
| arXiv 检索 | Atom XML 元数据 | [API manual](https://info.arxiv.org/help/api/user-manual.html)、[bulk data access](https://info.arxiv.org/help/bulk_data.html)：API 是元数据/检索，不能把 Atom entry 当全文 XML |
| arXiv 正文 | PDF；部分文章 HTML；TeX/LaTeX source；历史 PS/DVI 等 | [Viewing submissions](https://info.arxiv.org/help/view.html)：格式按文章可用，Source 可为 gzip 压缩单 TeX 文件或多文件 tar.gz；不承诺每篇都有 HTML 或源文件 |
| arXiv 批量 | PDF/source 批量 TAR，source 通常内含 tar.gz；XML manifest | [Full Text via S3](https://info.arxiv.org/help/bulk_data_s3.html)：S3 requester-pays，源文件多为含图 TeX/LaTeX；批次容器、单篇源包、论文文件、manifest 属于不同层，本次没有访问收费 bucket |
| DOI.org | 通常重定向 landing page；内容协商返回注册机构的元数据格式 | [Crossref 内容协商](https://www.crossref.org/documentation/retrieve-metadata/content-negotiation/)、[DataCite 内容协商](https://support.datacite.org/docs/datacite-content-resolver)：DOI 是解析/协商入口，不是通用全文库 |
| Crossref | REST JSON；XML API/OAI-PMH；CSL JSON、RDF/XML、Turtle、BibTeX、RIS、格式化引用、UNIXREF/UNIXSD XML | [Metadata Retrieval](https://www.crossref.org/documentation/retrieve-metadata)、[Content negotiation](https://www.crossref.org/documentation/retrieve-metadata/content-negotiation/)：均是元数据；[全文 URI 文档](https://support.crossref.org/hc/en-us/articles/214298866-Full-Text-URIs-Technical-Details)强调 PDF/XML/HTML 全文由 publisher 交付和控制权限 |
| DataCite | 内容协商支持 Turtle、CSL JSON、Schema.org JSON-LD、Codemeta JSON-LD、格式化引用、RIS、BibTeX、DataCite XML/JSON、JATS | [Supported Content Types](https://support.datacite.org/docs/datacite-content-resolver#supported-content-types)：该页明确均是 metadata content types，所以即使 MIME 为 `application/vnd.jats+xml` 也不能自动标记全文；服务无须认证 |

## 对共享契约的含义（设计推论，不是出版商已有协议）

1. 分别记录材料角色（检索响应、元数据、正文、source、附件、预览）和媒体类型；JSON/XML 不能单独表达“全文”。
2. 对 XML 加可选 dialect/schema 标识：Atom、PAM、JATS、BITS、Elsevier XOCS/DTD、IEEE 未确认；未知格式允许保留原样，不强行贴 JATS 标签。
3. 分开记录原始 bytes、内容类型、压缩/容器格式、原始 URL/最终 URL、provider、operation/view、文献 id/version，以及 available/entitled/retrieved/validated 等阶段。外链存在不等于已经取回全文。
4. 只有验证成功的正文才进入 Reader 全文管线；摘要回退、登录页、JSON 错误、第一页 PNG 不能冒充全文。
5. 当前证据仍需账号级验证：Elsevier PDF 权限、Springer TDM 开通和端点版本、IEEE 付费响应与 XML 方言，以及各文章的 HTML/source 可用性。
