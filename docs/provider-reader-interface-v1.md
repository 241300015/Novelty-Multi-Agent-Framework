# Provider—Reader 共享接口 v1

状态：**arXiv 已接通原件获取、Researcher 与 Reviewer 的文本读取和引用回查。** 共享模型位于 [schemas/materials.py](../backend/src/novelty_agent_framework/schemas/materials.py) 与 [schemas/reader.py](../backend/src/novelty_agent_framework/schemas/reader.py)。格式依据见 [Provider 原始材料盘点](provider-material-contract.md)。

默认工作流通过候选和制品菜单选择材料，Reader 在内部维护 open/read 句柄。

## 1. 分工与获取顺序

- **Provider 负责获取并保存原始文件、核验响应及文献身份、登记原始材料 Manifest，然后返回文件引用。** 可以复用注入的公共存储组件，保存职责仍由 Provider 完成。调用者不再拿 `FullText.text` 替 Provider 落盘。
- **Reader 按需解析。** 获取成功由原件的获取与登记决定。Researcher 或 Reviewer 选中文献正文后，Reader 验证来源和版本，再打开原件、分页读取并登记文本引用。
- **Harness 负责可信参数、预算、选择材料、处理失败和调度。** 模型不提供任意路径或下载 URL。
- 证据绑定实际读到的表示及其哈希，保留到原文件的来源链；Validator/Compiler 后续通过兼容层接入，不能直接套用原文件偏移。

| 来源 | 全文获取顺序 | 限定 |
| --- | --- | --- |
| arXiv | HTML → PDF | 版本必须一致；无 HTML 可转 PDF，不把 `/abs/` 页当正文 |
| ScienceDirect | Elsevier XML → PDF → 上游原生纯文本 | 每次尝试需有对应权限；FULL 显式视图。PDF/纯文本为可用性回退，不把不完整 JSON 当原生全文 |
| Springer | JATS/BITS XML；无可用正文时明确 unavailable | 不预设已经实现 PDF/HTML 外链下载；后续扩展单独声明 capability |
| IEEE | XML → JSON | XML 为工程首选，不宣称官方证明其更完整；解析 schema 与正文覆盖须实样验证，两者必须对应同一文献版本 |
| Crossref / DataCite / DOI.org | JSON 元数据 | 不提供全文；不自动追随全文链接扩大获取范围 |
| ChinaXiv | 暂不注册获取 capability | PDF 是后续候选，需先确认获取接口 |

顺序由配置/Harness 注入的 `format_preferences` 固定，Provider 只能在已声明能力中按序执行。网络临时错误按已有重试规则处理；遇到未授权或预算耗尽停止，不以换格式绕过权限。某格式明确不存在、接口不支持或返回无效材料时，可以在权限和剩余预算内尝试下一格式，并保留每次记录。不同数据库之间的回退不属于本接口。

摘要、元数据和搜索仍需要解析字段；“保存原始文件”删除的是 Provider 的强制全文转纯文本步骤，不删除检索解析。既有 `SearchTool`、`MetadataTool`、DOI 查询接口保持独立。

## 2. 公共规则与引用

下面列出的字段默认必填；`?` 表示可空，未观测到的值为 `null`；列表无项目时为 `[]`。JSON 使用 `snake_case`，所有请求/顶层结果/持久化描述均有 `schema_version: 1`。用于计算 binding/config/read_id 的规范 JSON 统一采用 UTF-8、键排序、无额外空格、保留 null、ensure_ascii=false；不是把原始响应重序列化。未知字段在实现时拒绝，新增语义通过契约版本协调。

| 公共类型 | 字段与规则 |
| --- | --- |
| `MaterialScope` | `subject_paper_id: str`；`namespace: research_reference \| subject_reference`，复用现有 ArtifactNamespace |
| `MaterialRef` | `subject_paper_id, namespace, artifact_id, sha256`；sha256 为实际保存字节的 64 位小写十六进制摘要。引用始终同时限定论文、命名空间、材料和版本，不使用裸 artifact_id |
| `DocumentTarget` | `work_id, source_record_id, external_id: str`；`requested_version: str?`。均来自已登记的来源记录，不能从模型自由输入构造 |
| `MaterialRole` | `search_response \| metadata \| abstract \| full_text \| supplement`。角色不是文件格式；只有全文主体核验通过才标 full_text |
| `FormatFamily` | `html \| pdf \| xml \| json \| text`。未知响应可留诊断文件，但不注册为可供读取的成功材料 |
| `ContentExtent` | 复用 `full \| partial \| unknown`，描述指定 role 的内容覆盖。完整摘要是 abstract/full，不是 full_text；HTTP 200、下载完成不能证明全文完整 |
| `issues` | 复用既有 `FailureEvent[]`、集中 FailureCode 目录。失败需要明确原因；正常 not_found/unsupported 通过 status 表达，不伪造异常 |

文件路径只出现在持久化描述中，不作为 Reader 的外部输入。`relative_path` 必须是对应论文及 namespace 存储根下的安全相对路径；拒绝绝对路径、`..` 和越界符号链接。根目录由组合根注入，不从上游或模型输出读取。

## 3. Provider 接口

```python
class MaterialProvider(Protocol):
    source_id: str
    async def acquire(self, request: MaterialAcquisitionRequest) -> MaterialAcquisitionResult: ...
```

### MaterialAcquisitionRequest

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| schema_version | Literal[1] | 契约版本 |
| request_id | str | 本次逻辑获取的唯一标识，关联 fallback/retry；不等于永久缓存 key |
| run_id | str | 运行溯源 |
| scope | MaterialScope | 输出所属论文和 namespace |
| source_id | str | 必须与 Provider 实例一致 |
| target | DocumentTarget | 已发现文献的可信身份与可选指定版本 |
| requested_role | metadata / abstract / full_text / supplement | 此次请求用途；既有搜索接口另行保存 search_response |
| format_preferences | FormatFamily[] | 非空、去重、有序；按上表配置，禁止 Provider 私自增加格式 |
| allow_abstract_fallback | bool | 默认 false；只有 true 才允许全文失败后以摘要结果返回 partial |
| max_bytes | int > 0 | 单个完整响应体经 HTTP 传输解码后的最大字节数；超限中止，不能截断后称原文件保存成功 |

请求受既有 Runtime 的物理请求预算、取消与超时控制；不在这个 DTO 内重建一套独立运行预算。

### MaterialAcquisitionResult

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| schema_version, request_id | 1, str | 与请求一致 |
| status | acquired / partial / not_found / unavailable / unauthorized / unsupported / failed | 详见下方 |
| materials | RawMaterialArtifact[] | 仅列已持久化、校验并登记成功的材料；通常首个合格格式成功后停止，不下载全部格式 |
| attempts | AcquisitionAttempt[] | 实际尝试的有序记录，包含失败和回退 |
| issues | FailureEvent[] | 具体原因，可包含已恢复的失败，不能只返回 None |

- `acquired`：至少一份材料满足请求 role、身份和版本要求，并完成文件保存与 Manifest 登记；content_extent 可以 unknown，不等于宣称全文已穷尽。
- `partial`：取得可用但已知缺失的材料，或请求全文且明确允许摘要降级。materials 必须非空；不能用 partial 掩盖未完成下载。
- `not_found`：明确未找到目标；`unavailable`：目标存在但没有请求的材料；`unauthorized`：权限或认证不满足；`unsupported`：Provider 不提供该用途/格式；`failed`：网络、协议、身份/版本不一致或落盘失败。
- 所有返回引用的 subject_paper_id、namespace、source_id 必须与请求一致，binding 必须对应 target；不得把其他任务或论文的已存材料混入返回。
- 后五种状态 materials 必须为空。HTTP 错误、验证码/登录页、响应类型错误和身份不一致不能登记为摘要/全文。诊断文件可以另存，但不出现在 materials。
- 无指定版本时保存观测版本，未知为 null；指定版本无法确认匹配时失败，不能静默用 latest。观察到身份矛盾的材料不能按请求 ID 强行绑定。

`AcquisitionAttempt` 字段：`attempt_id, requested_format, status`（acquired / unavailable / not_found / unauthorized / unsupported / failed）、`http_status: int?`、`artifact_refs: MaterialRef[]`、`issues: FailureEvent[]`。一次实际 HTTP 请求一条记录，已完成响应的 URL 等信息写在对应材料 provenance，失败请求的脱敏诊断沿用 Runtime。缓存命中不新增物理请求记录，可返回空 attempts。

### RawMaterialArtifact：Provider 保存并返回的描述

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| schema_version | 1 | 版本 |
| ref | MaterialRef | 原文件身份与字节哈希 |
| representation | Literal["raw"] | 未做字符转换、去标签、重排或裁剪 |
| role | MaterialRole | 文件用途；批量检索响应为 search_response |
| format | MaterialFormat | 见下表 |
| relative_path | str | 保存的原始文件路径，不能把文本改后缀冒充 XML/PDF |
| byte_size | int > 0 | 落盘文件实际字节数 |
| transfer_complete | Literal[true] | 只有完整取得的响应体进入成功材料；部分 HTTP Range 请求不作为完整响应材料接入 v1 |
| content_extent | ContentExtent | 响应内指定内容的覆盖，不是下载完成标记 |
| provenance | AcquisitionProvenance | 来源、操作、时间和 HTTP 事实 |
| bindings | RawRecordBinding[] | 单篇通常 1 条；一个原响应可绑定多篇，不重复伪装成多个单篇原文件 |
| warnings | str[] | 可诊断限制，不能代替失败状态 |

`MaterialFormat`：`family: FormatFamily`、`media_type: str`（验证后、无 charset 参数的媒体类型）、`declared_content_type: str?`（原响应头，保留参数）、`charset: str?`（实际确定的文本字符编码，PDF 为 null）、`schema_hint: str?`（如 `arxiv_html`、`atom`、`jats`、`bits`、`elsevier_xocs`、`ieee_fulltext`）。schema_hint 为可扩展的路由提示，不冒充已验证的 schema；Reader 必须核对实际结构。XML、JSON 不用后缀推断用途。

`AcquisitionProvenance`：`source_id, run_id, request_id, attempt_id, operation: str`、`view: str?`、`request_url, final_url: str`（均脱敏）、`http_status: int`、`acquired_at: timezone-aware ISO8601`、`content_encoding: str?`、`etag: str?`、`last_modified: str?`。不存 Cookie、认证头、API key。没有可信 landing URL 时不得以请求端点假装论文网页。

这里“原始字节”指 HTTP 客户端完成 transfer/content decoding 后的响应体，例如解除了 HTTP gzip，但没有字符解码或内容加工；保存 hash 覆盖这些实际字节。下载的 tar.gz 若是资源本身而不是 HTTP 编码，不能自动拆包并仍标 raw；归后续容器能力扩展。

`RawRecordBinding`：`work_id, source_record_id, external_id: str`、`observed_version: str?`、`selector: RecordSelector`、`binding_sha256: str`。binding_sha256 为除自身外这些 binding 字段的规范 JSON 的 SHA-256（UTF-8、键排序、无额外空格、保留 null、ensure_ascii=false）；绑定变化会改变哈希。selector 是该记录在这个响应中的位置，不是引文位置：

- `{"kind":"document"}`：整个单篇文件。
- `{"kind":"json_pointer","value":"/records/0"}`：RFC 6901 指向记录对象。
- `{"kind":"xml_xpath","value":"/a:feed/a:entry[1]","namespaces":{"a":"http://www.w3.org/2005/Atom"}}`：带 namespace 的明确记录节点。
- `{"kind":"html_xpath","value":"/html/body/ol/li[1]"}`：HTML DOM 节点，须验证与记录身份一致；DOM 解释规则由双方共享样本固定，无法匹配不能改用整页。

同一文件可被多个 binding 引用；`source_record_id` 在 bindings 中唯一。批响应的原文件可以没有单篇 work_id，work_id 位于各 binding。现有单篇 `Artifact` 不直接承担这个新结构，不能为了兼容给批响应编造 work_id。

## 4. Reader 接口（既有 v1 草案，本轮不调整）

本节记录已存在的接口及模型，供历史调用保持兼容；不表示获取阶段需要生成解析文件，也不提前决定后续统一消费方案。

```python
class MaterialReader(Protocol):
    async def open(self, request: MaterialOpenRequest) -> MaterialOpenResult: ...
    async def read(self, request: MaterialReadRequest) -> MaterialReadResult: ...
```

分开 open 和 read，是为了只解析一次、多次续读，并固定解析版本。Reader 的解析器选择来自实际媒体类型/schema_hint 和本地注册能力，不由模型指定任意代码。

### 打开与解析

`MaterialOpenRequest`：`schema_version=1, request_id, run_id, raw_ref: MaterialRef, source_record_id: str, binding_sha256: str, max_output_chars: int>0`。Reader 校验引用与实际文件哈希，从 bindings 找到唯一目标并校验 binding_sha256，再解析。`max_output_chars` 是解析表示的上限，不改变原文件；达到上限必须返回 partial。

`MaterialOpenResult`：`schema_version=1, request_id, status: ready|partial|unsupported|failed, document: ParsedMaterialArtifact?, issues: FailureEvent[]`。

- ready/partial 必须有已保存且可读取的 document；unsupported/failed 的 document 为 null。
- 原文件缺失、哈希不符、身份/版本或 selector 不符为 failed；未知格式/profile 或当前 Reader 没有处理能力为 unsupported。
- 不以空文本冒充解析成功。不联网下载 PDF 来补救 HTML 解析失败；由 Harness 决定是否重新调用 Provider。
- 解析包括正文结构、表格/公式等可读表示的选择。无法处理的内容必须计入 warnings；已知内容丢失返回 partial，而不是 ready。PDF 是否启用 OCR 属于版本化 parser 配置，并在 warnings/identity 中记录。

`ParsedMaterialArtifact` 字段：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| schema_version, representation | 1, Literal["parsed"] | 新的独立表示 |
| ref | MaterialRef | Reader 保存的读取文本文件身份和哈希；不能复用 raw_ref |
| source_ref | MaterialRef | 原文件及其固定哈希 |
| work_id, source_record_id | str | 必须与选中的 binding 一致 |
| binding_sha256 | str | 固定本次采用的记录选择器、身份和版本绑定 |
| role | metadata / abstract / full_text / supplement | 解析目标的语义，不因变成文本而丢掉用途 |
| parser | ParserIdentity | `name, version, config_sha256`；配置哈希包括影响输出的提取/OCR/规范化规则及 max_output_chars |
| media_type | text/plain 或 text/markdown | 实际读取表示的类型，不继续声称是源 XML/PDF |
| relative_path, byte_size | str, int>0 | 不可变 UTF-8 文本文件；保留确切空白，不在读取时 trim |
| char_count | int>0 | 按 Unicode code point 计数，对应 Python str 长度，不是字节或 UTF-16 单元 |
| content_extent | ContentExtent | 不得比来源更乐观；已知解析缺失/截断为 partial；未知保持 unknown |
| source_map | IndexFileRef | `relative_path, sha256, byte_size, schema_version=1`；索引持久化并校验 |
| created_at | timezone-aware ISO8601 | 创建时间 |
| warnings | str[] | 公式、表格、图片、OCR 或结构解析的已知限制 |

解析缓存必须至少按 `(raw_ref, source_record_id, binding_sha256, parser.name, parser.version, parser.config_sha256)` 区分；原文、selector、解析器或输出配置变化必须生成新表示。bindings 及其 selector 由 binding_sha256 固定；改变绑定不能复用旧解析结果或覆盖已被引用的历史绑定。

### Source map 与精度

source_map 顶层字段：`schema_version=1, parsed_ref, source_ref, entries: SourceMapping[]`。索引内容属于独立文件，不能覆盖来源哈希；Reader 校验索引引用与 ParsedMaterialArtifact 一致。

`SourceMapping`：`parsed_char_start, parsed_char_end: int`（全局读取表示的半开区间）、`precision: exact|structural|document`、`locator: SourceLocator`。允许一段文字对应多个定位项；位置必须合法，非空解析文字至少有 document 级回指。映射条目可覆盖章节/段落，不要求声称逐字符对应；exact 只能在实现确实能证明时使用。

`SourceLocator` 的 v1 形式：

| kind | 额外字段 | 含义 |
| --- | --- | --- |
| document | 无 | 只确认到原文件；precision=document |
| text_chars | start, end, charset | 源纯文本按指定编码解码后的字符范围；只用于原生 text，不用于 HTML 源码伪装成正文偏移 |
| xml_xpath | value, namespaces | 原 XML 的元素/文本位置，通常 structural；绑定解析器版本 |
| html_xpath | value | 原 HTML 经固定解析器建树后的节点，通常 structural |
| json_pointer | value | 原 JSON 中正文或摘要字段，通常 structural |
| pdf_page | page: int>=1 | 原 PDF 第几页，precision=structural；v1 不承诺坐标框或逐字节对应 |

由 Reader 添加的标题、分隔符或规范化文本不能编造原文范围；可降为 structural/document。索引不能映射到不同论文、namespace 或原文件哈希。

### 分段读取

`MaterialReadRequest`：`schema_version=1, request_id, run_id, parsed_ref: MaterialRef, char_start: int>=0, max_chars: int[1,16000]`。沿用现有单次读取上限，部署可更低。Reader 只接受登记过的 parsed_ref，不接受任意路径或原 HTML 直接冒充 parsed。

`MaterialReadResult`：`schema_version=1, request_id, status: content|eof|failed, read: MaterialReadSlice?, issues: FailureEvent[]`。

`MaterialReadSlice`：`read_id, parsed_ref, source_ref, source_record_id, work_id, role, media_type, char_start, char_end, text, has_more, origins: SourceMapping[]`。

必须满足：

1. `text == persisted_text[char_start:char_end]`，`char_end-char_start == len(text)`；保持空白，不能返回临时清洗后的另一段文本。
2. `0 <= char_end-char_start <= max_chars`，`char_end <= char_count`，`has_more == (char_end < char_count)`。起点等于 char_count 返回 eof、空字符串、空 origins；超过则 failed，不静默移动起点。
3. content 必须非空，eof 必须空；failed 的 read 为 null，原因在 issues。
4. origins 只返回与本次区间相交的映射，parsed 区间裁剪到本次范围；结构定位可以比片段粗。exact 映射若不能安全裁剪须降为 structural，不制造精确坐标。
5. `read_id` 使用 `read_` 加规范 JSON `{parsed_ref, source_map_sha256, char_start, char_end}` 的 SHA-256 前 24 位；同一结果的重复读取可复用。
6. 原文件、解析文件、source_map 任一哈希或绑定核验失败均不能返回成功读取。新证据的 quote/字符定位针对 parsed_ref，再通过 source_ref/origins 回指原文件。

## 5. 登记、兼容与双方不可省略的要求

原文件、描述和必要索引完成原子保存并登记后才能返回成功；文件存在但未登记不是成功。相同 artifact_id 不得对应不同字节。Provider 管理 raw 文件生命周期；Reader 只能新增 parsed 文件和索引，不能改 raw 描述或覆盖历史内容。

与现有接口的关系：

- 序列化类型统一使用 `schemas/materials.py`；双方直接导入同一版本，不复制私有 DTO。行为接口位于 `ports/materials.py`；arXiv 已通过能力工厂绑定存储并接入，其他来源仍使用旧兼容路径。
- 不修改旧 `FullText`、`Artifact`、`ReferenceReadResult` 的字段含义，不把 raw XML 放入旧 `FullText.text` 继续冒充已提取文本。
- 其他数据库全文仍保留旧路径；arXiv 原文件通过获取侧 `MaterialAcquisitionResult.materials` 返回，不作为旧 Reader 的可读 artifact_ids 暴露。旧 `.txt` 继续可读。JSON 摘要和 arXiv 正文统一通过 Reader 适配，Reader 从已登记的原件生成派生文本与引用。
- 兼容适配层将新 read 的 parsed_ref 注册为旧证据链可核验的文本 Artifact，同时保留 raw/source-map 链；未完成适配不能宣称已具备完整工作流兼容性。
- Provider 可抓取元数据字段建立 bindings；Reader 负责正文解析。某 PDF/网页无法由可信响应确认身份时不得随意给出绑定，可返回明确 failure。
- source_map 索引版本与解析器版本独立记录。共享样本中固定 HTML DOM selector 的解释方式；不同 Parser 不得默默复用相同 html_xpath 语义。

## 6. 验收矩阵与可复制样例

配套 [JSON 示例](provider-reader-interface-v1.example.json) 是合成示例，包含原 HTML、解析文本、精确哈希和 acquire/open/read 的请求与结果；不代表 arXiv 实网返回，也不是接口的自动实现。

双方共同验收：原始文件字节/哈希不变；成功返回必已落盘；批响应选择正确记录；版本不一致被拒绝；全文请求不把摘要冒充成功；XML/HTML/PDF/JSON/原生文本各有样本；Reader 不联网；相同原文和 parser 配置生成稳定读取表示；截断正确标 partial；字符范围与实际 quote 一致；原文/解析/索引任一篡改被发现；读取到 EOF 与越界区分；未知 schema 明确 unsupported；来源原来只有 abstract 时无法升级成 full_text。

数据模型与约束已落实到 `schemas/materials.py`；对应校验见 [test_material_schemas.py](../tests/test_material_schemas.py)。获取链独立保存原件。Researcher 和 Reviewer 在读取时调用 Reader，历史派生文件仍可回查。

### 已实现校验与运行时边界

- 模型拒绝未知字段，nullable 字段须显式传 null，空列表须显式传 []；仅 `allow_abstract_fallback` 默认 false。
- 已校验格式优先级非空且不重复、绑定规范 JSON 哈希、binding/attempt/artifact 身份唯一、成功/失败状态与有效载荷、带时区时间、安全相对路径、raw/parsed 同 scope 且不同 artifact_id、非空解析结果、UTF-8 字节数与字符数的基本一致性、读取文本的确切字符跨度、裁剪后的 origins、EOF 及定位精度。失败状态必须提供既有 FailureEvent。
- 路径统一为 POSIX 相对路径，拒绝空段、`.`、`..`、反斜杠、冒号和控制字符。绑定字段、JSON pointer 和读取文本不做隐式 trim，避免改变哈希或字符偏移。
- 顶层 source-map 模型命名为 `SourceMap`；六种 locator 及 `RecordSelector` / `SourceLocator` 使用 kind 判别联合。`canonical_material_json` 统一提供 binding/config/read_id 的规范字节编码。
- DTO 不进行网络或磁盘 I/O。请求/结果关联、target/版本与绑定一致、完整文件字节和哈希、Manifest 登记、路径解析后的符号链接边界、实际 MIME 与正文结构、parser 配置哈希、read_id、完整 source-map 覆盖及引用一致性仍由运行时核验；通过模型校验本身不代表这些条件已经成立。


## 7. 原始材料落盘管理

获取侧布局（`references/` 与 `subject_references/` 按 namespace 分开）：

```text
references/
  list.json                         # Work / SourceRecord：元数据、abstract、原文件目录
  materials.json                    # MaterialManifest v1：原文件描述、版本、格式、来源和哈希
  documents/<work_id>/raw/
    <artifact_id>.html               # 或 .pdf，完整保存 HTTP 解码后的原始响应体
```

- **摘要**：所有结构化检索来源继续使用 `SearchHit.abstract`，完整内容落在 `list.json` 的 `source_records[].abstract`；没有摘要时为 null。不再生成摘要 `.txt` 或 `ArtifactRole.ABSTRACT`。同一记录原先缺少摘要时可补齐；已有非空摘要不因重复检索被覆盖，不跨版本/来源合并摘要。
- **元数据**：继续使用既有 Work/SourceRecord JSON 与独立元数据身份核验链路。本轮不修改 MetadataRetrievalService 或 Validator，不重复建立元数据文件。
- **全文**：当前已迁移 arXiv，按 HTML → PDF 获取原文件。`materials.json.raw[]` 是完整描述；`list.json.artifacts[]` 登记 role=full_text、material_representation=raw 的目录项，并关联 source_record_id。获取与登记完成后标 full_text_acquired，不要求 Reader 能提取文字。
- **获取返回值**：Provider 使用 `schemas/materials.py` 的 `MaterialAcquisitionResult.materials: RawMaterialArtifact[]` 返回原文件描述。检索编排仍返回原有 ResearchBundle，摘要在 source_records 中；原文件描述可从 materials.json.raw[] 获取。本轮 arXiv 的 artifacts 列表为空，避免原文件被现有 LLM 投影误当作文本读取句柄，同时保持既有工作流对 ResearchBundle 的反序列化兼容。
- **消费侧**：获取流程不构造/调用 LocalMaterialReader，不生成 parsed/*.txt 或 map，不自动注册 parsed 文本。Reviewer 选择正文后，Reader 按需生成并登记 `parsed/*.txt` 和来源映射。候选菜单、证据校验和 Reviewer checkpoint 共用持久化文本的读取编号。

同一文献按 work_id 聚合；不同来源记录、版本、格式或字节内容拥有各自不可变引用。指定版本、同 scope 与 binding 且文件校验通过时可复用；未指定版本的 latest 不永久缓存。同字节与绑定的重复获取不覆盖历史 provenance。

原文件先原子写入，再登记 materials.json，最后与 list.json 的已发现文献关联。文件存在但未登记不能返回获取成功；登记失败保留已保存原文件以供重试，不执行自动删除。单次描述或清单采用原子替换，并使用现有进程内写锁合并；当前不提供两个清单间的跨文件事务、跨进程写协调或自动垃圾回收。

`MaterialManifest` 字段仍为 schema_version、scope、raw、parsed；保留 parsed 是为了兼容此前已有描述。新获取只增加 raw，不修改或清空已有 parsed。旧 `.txt` 不搬迁、不删除、不补造来源。其他数据库的全文原格式迁移不在本次实现范围。

获取侧验收命令：

```bash
PYTHONPATH=backend/src:. python -m novelty_agent_framework.experiments.arxiv_material_smoke --arxiv-id 2402.03300v1 --output-root outputs/arxiv-acquisition-only-20261116
PYTHONPATH=backend/src:. python -m novelty_agent_framework.experiments.arxiv_material_smoke --arxiv-id 2402.03300v1 --format-preferences pdf --output-root outputs/arxiv-acquisition-only-20261116
```

脚本读取真实 arXiv 摘要页并复用现有题录适配器，验证摘要 JSON 与原文件登记，不调用 Reader 或 LLM，也不替代已有元数据真实性校验。
