"""arXiv 主站通道：绕开 export API 的检索、元数据与引文解析实现。

背景
----
arXiv 官方 API（``export.arxiv.org/api/query``）按出口 IP 限流。本项目所在网络
（南京移动）自 2026-08 起持续返回 429 / 读超时，而主站 ``arxiv.org`` 完全可用。
本模块用主站页面提供与 ``ArxivSearchTool`` 对齐的能力，由配置开关
``search_transport="web"`` 启用：

- ``ArxivWebSearchTool.search``：``/search/advanced`` 主题检索；
- ``ArxivWebSearchTool.resolve_identifier`` / ``search_known_item``：
  ``/abs/`` 精确解析与引文已知项检索，供参考文献 bootstrap 使用；
- ``ArxivWebMetadataTool.resolve``：元数据核验。

与官方 API 通道的差异（2026-09-15 实测）
----------------------------------------
- **ANDNOT 不可用**：高级检索带 ``ANDNOT`` 运算符直接 0 结果，因此排除项从服务端
  查询中剔除，改为本地过滤标题 + 摘要。代价是排除只在返回页内生效；落在
  ``all`` 其他字段（comments / journal-ref）上的排除词会漏掉 —— 结果偏宽，方向安全。
- **字段限定必须走高级检索**：主站内联 ``ti:`` / ``abs:`` 是字面量，不生效；
  翻译器改用 ``terms-N-field``（已验证 ``all`` / ``title`` / ``abstract``）。
- **按括号分组编译**：一个顶层 AND 操作数 = 一个槽位，OR 组与组内词级 AND 链
  整体保留（槽位内 AND 优先级高于 OR，实测与 API 语义一致）。
- **不做翻页**：只取首页 ``size`` 条，``limit`` 超过首页容量时按实际返回。
- **时延高**：搜索页冷请求 5~25 秒，默认超时放宽到 60 秒。

失败语义：通道故障抛出 ArxivWebChannelError；只有成功检索允许返回空结果。
"""

from __future__ import annotations

import html as html_module
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from .arxiv_scheduler import _runtime_recorder, retry_delay, provider_task_id
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ....ports import MetadataTool, SearchHit, SearchTool
from ....schemas import EvidenceSource, ExternalIdentifier, ParsedCitation

ARXIV_WEB_ROOT = "https://arxiv.org"
ARXIV_WEB_SEARCH_URL = f"{ARXIV_WEB_ROOT}/search/"
ARXIV_WEB_ADVANCED_URL = f"{ARXIV_WEB_ROOT}/search/advanced"
ARXIV_WEB_ABS_URL = f"{ARXIV_WEB_ROOT}/abs/"
ARXIV_WEB_PDF_URL = f"{ARXIV_WEB_ROOT}/pdf/"

#: 保守起点；只有真实间隔阶梯实验支持后才下调。
DEFAULT_MIN_INTERVAL_SECONDS = 8.0
#: 搜索页冷请求可达 25 秒，超时必须留足余量。
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_PAGE_SIZE = 25
#: 主站 ``size`` 只认这几档（分页控件的取值）。实测 2026-09-16：1 / 10 / 20 / 30
#: 一律 HTTP 400；调用方条数须向上取整到合法档位，400 会作为故障传播。
ALLOWED_PAGE_SIZES = (25, 50, 100, 200)
#: 单页上限。取 200 时主站约 28 秒才返回，别当默认值用。
MAX_PAGE_SIZE = ALLOWED_PAGE_SIZES[-1]
#: 高级检索 ``terms-N-*`` 子句上限。实测 5 个子句可用；超限时先按同字段
#: AND 合并，仍超限则按顺序丢弃（宁宽勿错，丢弃会放宽而非收紧结果集）。
MAX_QUERY_SLOTS = 8
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: API 查询字段 → 主站高级检索 field。只收录实测验证过的取值；
#: 其余字段（au / cat / jr 等）一律退回 ``all``，避免给出非法字段值。
_FIELD_MAP = {
    "all": "all",
    "ti": "title",
    "title": "title",
    "abs": "abstract",
    "abstract": "abstract",
}

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r'(?P<field>[A-Za-z_]+):"(?P<phrase>(?:[^"\\]|\\.)*)"'
    r'|(?P<barefield>[A-Za-z_]+):(?P<word>[^\s()"]+)'
    r"|(?P<op>ANDNOT|AND|OR)(?![A-Za-z0-9_])"
    r"|(?P<paren>[()])"
    r")"
)

_RESULT_BLOCK = re.compile(r'<li class="arxiv-result">')
_BLOCK_TITLE = re.compile(r'<p class="title is-5 mathjax">\s*(.*?)\s*</p>', re.DOTALL)
_BLOCK_AUTHORS = re.compile(r'<p class="authors">\s*(.*?)\s*</p>', re.DOTALL)
_BLOCK_ABSTRACT = re.compile(r'<p class="abstract mathjax">\s*(.*?)\s*</p>', re.DOTALL)
_BLOCK_SUBMITTED = re.compile(r"Submitted</span>\s*([^<]+)", re.DOTALL)
_BLOCK_DOI = re.compile(r'href="https://doi\.org/([^"]+)"')
_BLOCK_ABS_LINK = re.compile(r'href="https://arxiv\.org/abs/([^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_VERSION_RE = re.compile(r"v\d+$")
#: arXiv 结果页的摘要用 ``abstract-full`` / ``abstract-short`` 两个 span 切换；
#: 正文里还嵌着 ``search-hit`` 高亮 span，正则的非贪婪匹配会在第一个内层
#: ``</span>`` 处截断（实测只取到 9 个字符），必须按 span 嵌套深度取内容。
_SPAN_OPEN = re.compile(r"<span\b[^>]*>|</span>")
#: 两个 span 的末尾各挂一个折叠开关（``△ Less`` / ``▽ More``）。它就在被取的
#: span 内部，``_clean`` 只剥标签不剥文字，于是整句摘要末尾会带一个 ``△ Less``
#: ——实测主站落库的 9/9 篇摘要全部带（API 通道的摘要没有），它会被写进
#: SourceRecord.abstract 并进到候选清单，读起来像正文的一部分。
_ABSTRACT_TOGGLE = re.compile(r"[△▽▴▾]\s*(?:Less|More)\b")
#: 观测版本号。结果页只在摘要两个 span 的 id 里带版本
#: （``id="2607.05736v1-abstract-short"``），详情页在 ``og:url`` 里带。
#: API 通道的 external_id 是带版本的（``2504.14937v1``），主站必须同样取到：
#: 否则 (1) 丢掉「观测到的版本」这一事实；(2) 同一篇论文在两个通道下算出不同的
#: source_record_id——实测切换通道后清单里每篇多出一条记录，并让
#: ``adapt_hit`` 的 ``has no observed version`` 警告对 9/9 篇候选全部触发。
_RESULT_BLOCK_VERSION = re.compile(r'id="(\d{4}\.\d{4,5}v\d+)-abstract-')
_ABS_PAGE_VERSION = re.compile(
    r'property="og:url"\s+content="https://arxiv\.org/abs/(\d{4}\.\d{4,5}v\d+)"'
)


class ArxivWebChannelError(RuntimeError):
    """主站通道在重试后仍失败。"""


class ArxivWebNotFoundError(ArxivWebChannelError):
    """主站明确返回 404：该条目不存在，重试无意义。"""


def strip_version(arxiv_id: str) -> str:
    """去掉 arXiv ID 的版本后缀，如 ``2305.12345v2`` -> ``2305.12345``。"""

    return _VERSION_RE.sub("", arxiv_id.strip())


def _clean(value: str) -> str:
    return _WS.sub(" ", html_module.unescape(_TAG.sub("", value))).strip()


def _clean_abstract(value: str) -> str:
    """清洗摘要并去掉折叠开关残留（``△ Less`` / ``▽ More``）。

    必须在 ``_clean`` 之后调用：折叠开关的锚点已被剥掉，只剩一行裸文字。
    """

    return _ABSTRACT_TOGGLE.sub("", _clean(value)).strip()


def _observed_version(pattern: re.Pattern[str], html: str, arxiv_id: str) -> str:
    """取页面上观测到的版本化 ID；页面没给出该版本时退回无版本 ID。

    ``document_id`` 始终是无版本号形态（下游按它解析标识），只有 ``external_id``
    带版本——与 API 通道 ``arxiv.py`` 的约定保持一致。
    """

    matched = pattern.search(html)
    if matched is not None and strip_version(matched.group(1)) == arxiv_id:
        return matched.group(1)
    return arxiv_id


def _extract_span(html: str, class_token: str) -> str | None:
    """取第一个 class 含 ``class_token`` 的 ``<span>`` 的内层 HTML。

    按 span 嵌套深度配平，避免被内层高亮 span 提前截断。
    """

    opening = re.search(
        r'<span[^>]*class="[^"]*\b' + re.escape(class_token) + r'\b[^"]*"[^>]*>',
        html,
    )
    if opening is None:
        return None
    depth = 1
    position = opening.end()
    for tag in _SPAN_OPEN.finditer(html, position):
        if tag.group().startswith("</"):
            depth -= 1
            if depth == 0:
                return html[position : tag.start()]
        else:
            depth += 1
    return html[position:]


def _unescape_phrase(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _map_field(field: str) -> str:
    return _FIELD_MAP.get(field.strip().lower(), "all")


# --------------------------------------------------------------------------
# 查询翻译：API 查询语法 → 主站高级检索参数
# --------------------------------------------------------------------------
#
# 主站高级检索（``/search/advanced``）实测语义（2026-09-15）：
#
# - 槽位之间用 ``terms-N-operator``（``AND`` / ``OR`` / ``NOT``）连接；``NOT``
#   等价于 ``A AND NOT B``。
# - 槽位内的术语文本同样支持 ``AND`` / ``OR``，且 **AND 优先级高于 OR**
#   （实测 ``A OR B AND C`` = |A| + |B∧C| = 标准优先级）。
# - ``ANDNOT`` 作为槽位运算符返回 0 结果（不可用）→ 排除项下沉为本地过滤。
#
# 真实 ``ArxivQueryAdapter`` 输出里，一个概念会被渲染成「圆括号包住的 OR 组」，
# 组内每个术语若超过 ``phrase_max_words`` 还会展开成词级 AND 链，例如::
#
#     (ti:"graph neural network" OR ti:message AND ti:passing AND ti:neural)
#       AND abs:"molecular property prediction"
#       ANDNOT (all:"survey" OR all:"review")
#
# 因此翻译器必须 **按括号分组**：一个顶层 AND 操作数 = 一个槽位，组内文本
# 原样保留（AND 优先级已经保证语义正确）；跨字段分组降级为 ``all``
# （放宽方向，宁宽勿错）。早期版本把 OR 组和词级 AND 链拍平成独立槽位，
# 结果集被收窄到 0 条，这个 bug 就是分组成型的原因。


@dataclass(frozen=True)
class _Term:
    field: str
    text: str
    quoted: bool

    def render(self) -> str:
        return f'"{self.text}"' if self.quoted else self.text


@dataclass(frozen=True)
class _TermNode:
    term: _Term


@dataclass(frozen=True)
class _GroupNode:
    """``AND`` / ``OR`` 结点；``AND`` 与 ``OR`` 已按优先级分层，不再拍平混用。"""

    joiner: str
    operands: tuple["_Node", ...]


@dataclass(frozen=True)
class _NotNode:
    """``ANDNOT`` 产生的一元否定，编译期剥离为本地排除词。"""

    operand: "_Node"


_Node = _TermNode | _GroupNode | _NotNode


@dataclass(frozen=True)
class _Clause:
    """一个服务端槽位：字段 + 槽位内文本。

    ``single`` 表示槽位内恰好一个术语 —— 只有这种槽位允许超限时与相邻同字段
    槽位 AND 合并（多个术语混在一个槽位里的语义未经验证，不合并）。
    """

    field: str
    text: str
    single: bool = False


@dataclass(frozen=True)
class MainSiteQueryPlan:
    """一条主站检索请求：服务端参数 + 需要本地执行的排除项。"""

    api_query: str
    params: dict[str, Any]
    excludes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _tokenize(query: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    position = 0
    while position < len(query):
        match = _TOKEN_RE.match(query, position)
        if match is None:
            if not query[position:].strip():
                break
            fragment = query[position : position + 24].split(maxsplit=1)[0]
            raise ValueError(f"无法解析的 arXiv 查询片段：{fragment!r}")
        position = match.end()
        if match.group("paren"):
            tokens.append(("paren", match.group("paren")))
        elif match.group("op"):
            tokens.append(("op", match.group("op")))
        elif match.group("phrase") is not None:
            tokens.append(
                (
                    "term",
                    _Term(
                        field=_map_field(match.group("field")),
                        text=_unescape_phrase(match.group("phrase")),
                        quoted=True,
                    ),
                )
            )
        else:
            tokens.append(
                (
                    "term",
                    _Term(
                        field=_map_field(match.group("barefield")),
                        text=match.group("word"),
                        quoted=False,
                    ),
                )
            )
    return tokens


class _QueryParser:
    """递归下降解析器：``AND`` 优先级高于 ``OR``，``ANDNOT`` 是一元否定。"""

    def __init__(self, tokens: Sequence[tuple[str, Any]]) -> None:
        self._tokens = list(tokens)
        self._position = 0

    def parse(self) -> _Node:
        if not self._tokens:
            raise ValueError("查询表达式为空")
        node = self._parse_or()
        if self._position < len(self._tokens):
            kind, value = self._tokens[self._position]
            raise ValueError(f"查询表达式存在多余 token：{value!r}（{kind}）")
        return node

    def _peek(self) -> tuple[str, Any] | None:
        if self._position < len(self._tokens):
            return self._tokens[self._position]
        return None

    def _parse_or(self) -> _Node:
        operands = [self._parse_and()]
        while self._peek() == ("op", "OR"):
            self._position += 1
            operands.append(self._parse_and())
        if len(operands) == 1:
            return operands[0]
        return _GroupNode("OR", tuple(operands))

    def _parse_and(self) -> _Node:
        operands: list[_Node] = [self._parse_unary()]
        while True:
            token = self._peek()
            if token is None or token[0] != "op" or token[1] not in ("AND", "ANDNOT"):
                break
            negate = token[1] == "ANDNOT"
            self._position += 1
            operand = self._parse_unary()
            operands.append(_NotNode(operand) if negate else operand)
        if len(operands) == 1:
            return operands[0]
        return _GroupNode("AND", tuple(operands))

    def _parse_unary(self) -> _Node:
        if self._peek() == ("op", "ANDNOT"):
            self._position += 1
            return _NotNode(self._parse_unary())
        return self._parse_atom()

    def _parse_atom(self) -> _Node:
        token = self._peek()
        if token is None:
            raise ValueError("查询表达式在运算符后意外结束")
        kind, value = token
        if kind == "term":
            self._position += 1
            return _TermNode(value)
        if kind == "paren" and value == "(":
            self._position += 1
            node = self._parse_or()
            if self._peek() != ("paren", ")"):
                raise ValueError("查询表达式括号不匹配")
            self._position += 1
            return node
        raise ValueError(f"无法解析的查询片段：{value!r}")


def _leaf_terms(node: _Node) -> tuple[_Term, ...]:
    """收集子树里的全部术语（``_peel_nots`` 之后不应再有 NOT 结点）。"""

    if isinstance(node, _TermNode):
        return (node.term,)
    if isinstance(node, _GroupNode):
        return tuple(term for child in node.operands for term in _leaf_terms(child))
    raise ValueError("NOT 结点必须在使用前剥离")


def _peel_nots(node: _Node, notes: list[str]) -> tuple[_Node | None, list[str]]:
    """剥离 ``ANDNOT``：否定项收集为本地排除词，其余结构原地保留。

    与 AND 相连的否定（``A ANDNOT (x)``，适配器的唯一输出形状）剥离后语义
    不变。理论上 ``OR`` 分支里的否定剥离会收窄结果集，但适配器不产生这种
    形状；万一出现，留痕而不是静默改变语义。
    """

    if isinstance(node, _NotNode):
        return None, [term.text for term in _leaf_terms(node.operand)]
    if isinstance(node, _TermNode):
        return node, []
    operands: list[_Node] = []
    excluded: list[str] = []
    for child in node.operands:
        kept, terms = _peel_nots(child, notes)
        if terms and node.joiner == "OR":
            notes.append(
                f"OR 分支中的 ANDNOT（{'/'.join(terms)}）无法表达，已丢弃该否定"
            )
        excluded.extend(terms)
        if kept is not None:
            operands.append(kept)
    if not operands:
        return None, excluded
    if len(operands) == 1:
        return operands[0], excluded
    return _GroupNode(node.joiner, tuple(operands)), excluded


def _render_node(
    node: _Node, notes: list[str], *, inside_joiner: str | None = None
) -> str:
    """渲染槽位内文本。AND 优先级高于 OR，只有 OR 嵌在 AND 里才需要括号。"""

    if isinstance(node, _TermNode):
        return node.term.render()
    if isinstance(node, _NotNode):  # pragma: no cover - 由 _peel_nots 保证
        raise ValueError("NOT 结点必须在使用前剥离")
    inner = f" {node.joiner} ".join(
        _render_node(child, notes, inside_joiner=node.joiner)
        for child in node.operands
    )
    if node.joiner == "OR" and inside_joiner == "AND":
        notes.append("嵌套分组需要括号（主站槽位内括号语法未验证）")
        return f"({inner})"
    return inner


def _compile_operand(operand: _Node, notes: list[str]) -> _Clause:
    """一个顶层 AND 操作数 → 一个槽位。"""

    terms = _leaf_terms(operand)
    fields = {term.field for term in terms}
    field = terms[0].field
    if len(fields) > 1:
        notes.append(f"跨字段分组 {'/'.join(sorted(fields))} 降级为 all（结果更宽）")
        field = "all"
    return _Clause(
        field=field,
        text=_render_node(operand, notes),
        single=len(terms) == 1,
    )


def _compile_slots(root: _Node, notes: list[str]) -> list[_Clause]:
    """顶层 AND 树的每个操作数各占一个槽位；非 AND 顶层整体占一个槽位。"""

    if isinstance(root, _GroupNode) and root.joiner == "AND":
        operands: Sequence[_Node] = root.operands
    else:
        operands = (root,)
    return [_compile_operand(operand, notes) for operand in operands]


def _compact_clauses(clauses: Sequence[_Clause]) -> list[_Clause]:
    """超限压缩：相邻同字段的单术语槽位用 AND 并进同一槽位。

    只在两侧都是单术语时合并（保持一对一成对合并，避免把长 AND 链塞进一个
    未经验证的槽位），多术语槽位一律不动。
    """

    merged: list[_Clause] = []
    for clause in clauses:
        if merged and merged[-1].single and clause.single and (
            merged[-1].field == clause.field
        ):
            previous = merged[-1]
            merged[-1] = _Clause(
                field=clause.field,
                text=f"{previous.text} AND {clause.text}",
                single=False,
            )
        else:
            merged.append(clause)
    return merged


def translate_arxiv_query(
    query: str, *, page_size: int = DEFAULT_PAGE_SIZE
) -> MainSiteQueryPlan:
    """把 API 风格查询编译为主站 ``/search/advanced`` 参数。

    输入形如 ``(ti:"a" OR ti:"b") AND abs:"c" ANDNOT (all:"x")``（本项目的
    ``ArxivQueryAdapter`` 输出）。输出见 `MainSiteQueryPlan`：``excludes``
    是被剔除的 ANDNOT 术语，调用方必须本地过滤。
    """

    notes: list[str] = []
    root, negatives = _peel_nots(_QueryParser(_tokenize(query)).parse(), notes)
    clauses = _compile_slots(root, notes) if root is not None else []
    if not clauses:
        notes.append("查询全部由 ANDNOT 构成，主站无法表达，服务端条件为空")
    elif negatives:
        notes.append(
            f"ANDNOT 降级为本地过滤（{len(negatives)} 项，只在返回页内生效）"
        )
    if len(clauses) > MAX_QUERY_SLOTS:
        clauses = _compact_clauses(clauses)
        if len(clauses) > MAX_QUERY_SLOTS:
            notes.append(
                f"子句数 {len(clauses)} 超过槽位上限 {MAX_QUERY_SLOTS}，"
                f"丢弃 {len(clauses) - MAX_QUERY_SLOTS} 个尾部子句（结果更宽）"
            )
            clauses = clauses[:MAX_QUERY_SLOTS]

    params: dict[str, Any] = {
        "advanced": "",
        "classification-include_cross_list": "include",
        # abstracts=hide 会让结果块完全不含摘要，候选文献质量会大幅下降。
        "abstracts": "show",
        "size": page_size,
        "order": "-announced_date_first",
    }
    for index, clause in enumerate(clauses):
        params[f"terms-{index}-operator"] = "AND"
        params[f"terms-{index}-term"] = clause.text
        params[f"terms-{index}-field"] = clause.field
    return MainSiteQueryPlan(
        api_query=query,
        params=params,
        excludes=tuple(dict.fromkeys(term for term in negatives if term)),
        notes=tuple(notes),
    )


def _excluded(hit: SearchHit, excludes: Sequence[str]) -> bool:
    """ANDNOT 降级实现：标题 + 摘要的本地子串过滤（大小写不敏感）。"""

    if not excludes:
        return False
    haystack = f"{hit.title}\n{hit.abstract}".casefold()
    return any(term.casefold() in haystack for term in excludes if term)


# --------------------------------------------------------------------------
# HTTP 会话
# --------------------------------------------------------------------------


class _WebGate:
    """One process-wide emission gate, including circuit and server cooldown."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_dispatch: float | None = None
        self.min_interval = 0.0
        self.blocked_until = 0.0
        self.failures = 0
        self.open_until = 0.0
        self.state = "CLOSED"


_WEB_GATE = _WebGate()


def reset_shared_arxiv_web_gate() -> None:
    """Test isolation only; never reset a running workflow's gate."""
    global _WEB_GATE
    _WEB_GATE = _WebGate()


class ArxivWebCircuitOpenError(ArxivWebChannelError):
    """Circuit is OPEN; no HTTP was dispatched."""


class ArxivWebRetryBudgetExceeded(ArxivWebChannelError):
    """The total queue/throttle/retry budget was exhausted."""


class ArxivWebSession:
    """Bounded Web requests through a shared process-wide gate.

    The gate covers the actual client call, not just slot reservation. Independent
    sessions and full-text requests therefore cannot overtake a reserved slot.
    A HALF_OPEN probe has exactly one attempt, and queued callers recheck state.
    """

    def __init__(self, *, client: httpx.Client | None = None,
                 min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
                 circuit_cooldown_seconds: float = 60.0,
                 retry_budget_seconds: float = 90.0,
                 max_retry_delay: float = 10.0,
                 user_agent: str = DEFAULT_USER_AGENT) -> None:
        if min_interval < 0 or max_retries < 0 or max_retry_delay < 0:
            raise ValueError("interval and retry settings must be non-negative")
        if timeout <= 0 or retry_budget_seconds <= 0:
            raise ValueError("timeout and retry budget must be positive")
        if max_consecutive_failures < 1 or circuit_cooldown_seconds < 0:
            raise ValueError("invalid circuit settings")
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=False, headers={"User-Agent": user_agent}
        )
        self._min_interval = min_interval
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_consecutive_failures = max_consecutive_failures
        self._cooldown = circuit_cooldown_seconds
        self._budget = retry_budget_seconds
        self._max_retry_delay = max_retry_delay
        self._gate = _WEB_GATE
        with self._gate.lock:
            self._gate.min_interval = max(self._gate.min_interval, min_interval)
        self._events: list[dict[str, Any]] = []
        self._events_lock = threading.Lock()
        self.last_error: str | None = None

    def _emit(self, event_type: str, operation: str, **fields: Any) -> None:
        event = {"event_type": event_type, "provider": "arxiv", "transport": "web",
                 "operation": operation, "task_id": provider_task_id.get(), **fields}
        with self._events_lock:
            self._events.append(event)
        recorder = _runtime_recorder()
        if recorder is not None:
            recorder.record_provider_request(event)

    def _transition(self, state: str, operation: str) -> None:
        previous = self._gate.state
        self._gate.state = state
        self._emit("circuit_transition", operation, previous_state=previous, circuit_state=state)

    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> httpx.Response:
        operation = "search" if "/search" in url else "metadata" if "/abs/" in url else "full_text"
        logical_id = uuid.uuid4().hex
        self._emit("logical_request", operation, logical_request_id=logical_id)
        deadline = time.monotonic() + self._budget
        for redirect in range(6):
            response = self._get_request(url, params=params, deadline=deadline,
                                         logical_id=logical_id, operation=operation)
            if not response.is_redirect:
                return response
            location = response.headers.get("Location")
            target = urljoin(url, location) if location else ""
            if redirect == 5 or urlsplit(target).hostname != "arxiv.org" or urlsplit(target).scheme != "https":
                raise ArxivWebChannelError("invalid or excessive arxiv web redirect")
            url, params = target, None
        raise AssertionError("unreachable")

    def _get_request(self, url: str, *, params: Mapping[str, Any] | None,
                     deadline: float, logical_id: str, operation: str) -> httpx.Response:
        queued = time.monotonic()
        gate = self._gate
        if gate.state == "OPEN" and queued < gate.open_until:
            self._emit("circuit_rejected", operation, circuit_state="OPEN")
            raise ArxivWebCircuitOpenError("arxiv web circuit is open")
        if not gate.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise ArxivWebRetryBudgetExceeded("web request queue budget exhausted")
        try:
            if gate.state == "OPEN":
                if time.monotonic() < gate.open_until:
                    self._emit("circuit_rejected", operation, circuit_state="OPEN")
                    raise ArxivWebCircuitOpenError("arxiv web circuit is open")
                self._transition("HALF_OPEN", operation)
            probe = gate.state == "HALF_OPEN"
            attempts = 1 if probe else self._max_retries + 1
            for attempt in range(1, attempts + 1):
                now = time.monotonic()
                next_slot = (gate.last_dispatch + gate.min_interval) if gate.last_dispatch is not None else now
                wait = max(0.0, max(next_slot, gate.blocked_until) - now)
                if now + wait >= deadline:
                    if attempt > 1:
                        gate.failures += 1
                    if probe or gate.failures >= self._max_consecutive_failures:
                        gate.open_until = now + self._cooldown
                        self._transition("OPEN", operation)
                    self._emit("budget_exhausted", operation, logical_request_id=logical_id)
                    raise ArxivWebRetryBudgetExceeded("web throttle/retry budget exhausted")
                if wait:
                    time.sleep(wait)
                dispatched = time.monotonic()
                interval = None if gate.last_dispatch is None else (dispatched - gate.last_dispatch) * 1000
                gate.last_dispatch = dispatched
                request_id = uuid.uuid4().hex
                wall = datetime.now(timezone.utc).isoformat()
                response = None
                error = None
                delay = 0.0
                try:
                    recorder = _runtime_recorder()
                    if recorder is not None:
                        recorder.reserve_provider_request(provider="arxiv_web",
                                                          operation=operation)
                    response = self._client.get(url, params=params,
                        timeout=min(self._timeout, max(0.001, deadline - dispatched)),
                        follow_redirects=False)
                    if response.status_code != 200 and not response.is_redirect:
                        error = ArxivWebChannelError(f"HTTP {response.status_code} for {url}")
                except httpx.HTTPError as exc:
                    error = exc
                status = response.status_code if response is not None else None
                retryable = status in {429, 500, 502, 503, 504} or isinstance(error, httpx.TransportError)
                if retryable:
                    delay = (retry_delay(response, attempt - 1, max_delay=self._max_retry_delay)
                             if response is not None else min(2.0 ** attempt, self._max_retry_delay))
                    gate.blocked_until = max(gate.blocked_until, time.monotonic() + delay)
                self._emit("physical_request", operation, request_id=request_id,
                    physical_request_id=request_id, logical_request_ids=[logical_id],
                    dispatch_at=wall, previous_request_interval_ms=interval,
                    min_interval_seconds=gate.min_interval,
                    interval_violation=interval is not None and interval + 0.001 < gate.min_interval * 1000,
                    attempt=attempt, status_code=status,
                    elapsed_ms=(time.monotonic() - dispatched) * 1000,
                    queue_wait_ms=(dispatched - queued) * 1000,
                    retry_after=response.headers.get("Retry-After") if response is not None else None,
                    backoff_ms=delay * 1000, error_type=type(error).__name__ if error else None)
                if error is None or status == 404:
                    gate.failures = 0
                    gate.open_until = 0.0
                    if gate.state != "CLOSED":
                        self._transition("CLOSED", operation)
                    self.last_error = None
                    if status == 404:
                        raise ArxivWebNotFoundError(f"HTTP 404 for {url}")
                    return response
                self.last_error = f"{type(error).__name__}: {error}"
                if attempt == attempts or not retryable:
                    gate.failures += 1
                    if probe or gate.failures >= self._max_consecutive_failures:
                        gate.open_until = time.monotonic() + self._cooldown
                        self._transition("OPEN", operation)
                    raise ArxivWebChannelError(self.last_error) from error
                # A retry which cannot fit the budget is a failed logical call.
                if time.monotonic() + delay >= deadline:
                    gate.failures += 1
                    if gate.failures >= self._max_consecutive_failures:
                        gate.open_until = time.monotonic() + self._cooldown
                        self._transition("OPEN", operation)
                    self._emit("budget_exhausted", operation, logical_request_id=logical_id)
                    raise ArxivWebRetryBudgetExceeded(self.last_error) from error
        finally:
            gate.lock.release()
        raise AssertionError("unreachable")

    def stats(self) -> dict[str, Any]:
        with self._events_lock:
            events = list(self._events)
        physical = [e for e in events if e["event_type"] == "physical_request"]
        return {
            "transport": "web", "requests": len(physical),
            "logical_web_requests": sum(e["event_type"] == "logical_request" for e in events),
            "physical_web_requests": len(physical),
            "failed_requests": sum(e["error_type"] is not None for e in physical),
            "http_200_count": sum(e["status_code"] == 200 for e in physical),
            "http_429_count": sum(e["status_code"] == 429 for e in physical),
            "timeout_count": sum("Timeout" in (e["error_type"] or "") for e in physical),
            "retry_count": sum(e["attempt"] > 1 for e in physical),
            "interval_violation_count": sum(e["interval_violation"] for e in physical),
            "circuit_open_count": sum(e.get("circuit_state") == "OPEN" and e["event_type"] == "circuit_transition" for e in events),
            "circuit_state": self._gate.state, "consecutive_failures": self._gate.failures,
            "last_error": self.last_error, "events": events,
        }


# --------------------------------------------------------------------------
# HTML 解析
# --------------------------------------------------------------------------


def parse_search_page(html: str, *, limit: int = DEFAULT_PAGE_SIZE) -> list[SearchHit]:
    """解析 ``/search/`` 与 ``/search/advanced`` 的结果块（两页结构一致）。"""

    hits: list[SearchHit] = []
    for block in _RESULT_BLOCK.split(html)[1:]:
        if len(hits) >= limit:
            break
        id_match = _BLOCK_ABS_LINK.search(block)
        title_match = _BLOCK_TITLE.search(block)
        if id_match is None or title_match is None:
            continue
        arxiv_id = strip_version(id_match.group(1))
        title_text = _clean(title_match.group(1))
        if not title_text:
            continue
        authors: list[str] = []
        author_match = _BLOCK_AUTHORS.search(block)
        if author_match:
            authors = [
                _clean(item)
                for item in re.findall(
                    r"<a[^>]*>(.*?)</a>", author_match.group(1), re.DOTALL
                )
                if _clean(item)
            ]
        abstract_text = ""
        abstract_match = _BLOCK_ABSTRACT.search(block)
        if abstract_match:
            raw_abstract = abstract_match.group(1)
            full_abstract = _extract_span(raw_abstract, "abstract-full")
            abstract_text = _clean_abstract(
                full_abstract if full_abstract is not None else raw_abstract
            )
            if abstract_text.startswith("Abstract:"):
                abstract_text = abstract_text[len("Abstract:") :].strip()
        year = None
        submitted = _BLOCK_SUBMITTED.search(block)
        if submitted:
            year_match = re.search(r"(19|20)\d{2}", submitted.group(1))
            if year_match:
                year = int(year_match.group(0))
        doi = None
        doi_match = _BLOCK_DOI.search(block)
        if doi_match:
            doi = doi_match.group(1).strip()
        hits.append(
            SearchHit(
                document_id=arxiv_id,
                external_id=_observed_version(_RESULT_BLOCK_VERSION, block, arxiv_id),
                title=title_text,
                abstract=abstract_text,
                authors=tuple(authors),
                year=year,
                doi=doi,
                url=f"{ARXIV_WEB_ABS_URL}{arxiv_id}",
                full_text_url=f"{ARXIV_WEB_PDF_URL}{arxiv_id}",
                source_id="arxiv",
                raw_metadata={"channel": "arxiv-web-search"},
            )
        )
    return hits


def parse_abs_page(html: str, arxiv_id: str) -> SearchHit | None:
    """解析 ``/abs/<id>`` 详情页；标题缺失（异常页）返回 None。"""

    title = re.search(r'<h1 class="title mathjax">(.*?)</h1>', html, re.DOTALL)
    if title is None:
        return None
    title_text = _clean(title.group(1))
    if title_text.startswith("Title:"):
        title_text = title_text[len("Title:") :].strip()
    if not title_text:
        return None
    authors = re.search(r'<div class="authors">(.*?)</div>', html, re.DOTALL)
    abstract = re.search(
        r'<blockquote class="abstract mathjax">(.*?)</blockquote>', html, re.DOTALL
    )
    dateline = re.search(r'<div class="dateline">(.*?)</div>', html, re.DOTALL)
    author_text = _clean(authors.group(1)) if authors else ""
    if author_text.startswith("Authors:"):
        author_text = author_text[len("Authors:") :].strip()
    abstract_text = _clean_abstract(abstract.group(1)) if abstract else ""
    if abstract_text.startswith("Abstract:"):
        abstract_text = abstract_text[len("Abstract:") :].strip()
    year = None
    if dateline:
        matched = re.search(
            r"Submitted on (\d{1,2} \w+ (\d{4}))", _clean(dateline.group(1))
        )
        if matched:
            year = int(matched.group(2))
    doi = None
    doi_match = re.search(
        r'class="tablecell doi"[^>]*>\s*<a[^>]*href="https://doi\.org/([^"]+)"',
        html,
    )
    if doi_match:
        doi = doi_match.group(1).strip()
    return SearchHit(
        document_id=arxiv_id,
        external_id=_observed_version(_ABS_PAGE_VERSION, html, arxiv_id),
        title=title_text,
        abstract=abstract_text,
        authors=tuple(item.strip() for item in author_text.split(",") if item.strip()),
        year=year,
        doi=doi,
        url=f"{ARXIV_WEB_ABS_URL}{arxiv_id}",
        full_text_url=f"{ARXIV_WEB_PDF_URL}{arxiv_id}",
        source_id="arxiv",
        raw_metadata={"channel": "arxiv-web-abs"},
    )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


class ArxivWebSearchTool(SearchTool):
    """主站检索工具：主题检索 + 精确 ID 解析 + 引文已知项检索。"""

    source_id = "arxiv"
    channel = "web"

    def __init__(
        self,
        *,
        session: ArxivWebSession | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self._session = session or ArxivWebSession()
        self._page_size = page_size
        self._abs_cache: dict[str, SearchHit | None] = {}
        self._abs_lock = threading.Lock()
        self.last_error: str | None = None
        self.last_plan: MainSiteQueryPlan | None = None

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        """主题检索；通道故障传播给检索层并记为 FAILED。"""

        page_size = self._size_for(limit)
        plan = translate_arxiv_query(query, page_size=page_size)
        self.last_plan = plan
        if not any(key.endswith("-term") for key in plan.params):
            # 全部条件都被 ANDNOT 吃掉时不能空手打主站（那等于全库检索）。
            self.last_error = "主站查询不含任何正向条件，已跳过检索"
            raise ArxivWebChannelError(self.last_error)
        try:
            response = self._session.get(
                ARXIV_WEB_ADVANCED_URL, params=plan.params
            )
        except ArxivWebChannelError as exc:
            self.last_error = str(exc)
            raise
        self.last_error = None
        hits = parse_search_page(response.text, limit=page_size)
        self._prime_abs_cache(hits)
        kept = [hit for hit in hits if not _excluded(hit, plan.excludes)]
        return tuple(kept[:limit])

    def resolve_identifier(self, identifier: ExternalIdentifier) -> SearchHit | None:
        """按精确 arXiv ID 解析单篇文献；非 arxiv 命名空间返回 None。"""

        if identifier.namespace != "arxiv":
            return None
        return self.abs_hit(strip_version(identifier.value))

    def search_known_item(
        self, citation: ParsedCitation, *, limit: int = 5
    ) -> Sequence[SearchHit]:
        """按引文已知项检索：优先精确 arXiv ID，否则退回标题检索。

        标题检索走主站简单检索（``searchtype=all`` + 整标题短语），这条路径已在
        91 条参考文献的实测中验证。通道故障抛 ``ArxivWebChannelError``，让
        bootstrap 记 FAILED 而不是 not_found。
        """

        if citation.arxiv_id:
            hit = self.resolve_identifier(
                ExternalIdentifier(namespace="arxiv", value=citation.arxiv_id)
            )
            return (hit,) if hit else ()
        title = (citation.title or "").strip()
        if not title:
            return ()
        # 标题内嵌引号会破坏短语语法，直接剔除而不是转义（转义语法未经验证）。
        phrase = '"' + title.replace('"', " ") + '"'
        response = self._session.get(
            ARXIV_WEB_SEARCH_URL,
            params={
                "searchtype": "all",
                "query": phrase,
                "size": self._size_for(limit),
            },
        )
        return tuple(parse_search_page(response.text, limit=limit))

    def stats(self) -> dict[str, Any]:
        return {**self._session.stats(), "abs_cache_entries": len(self._abs_cache)}

    def abs_hit(self, arxiv_id: str) -> SearchHit | None:
        """按 ID 抓 ``/abs/`` 详情页（带缓存）；不存在返回 None，通道故障抛错。

        同一来源里的元数据工具复用它，避免同一次构建里对同一 ID 抓两次。
        """

        key = strip_version(arxiv_id)
        with self._abs_lock:
            if key in self._abs_cache:
                return self._abs_cache[key]
        try:
            response = self._session.get(f"{ARXIV_WEB_ABS_URL}{key}")
        except ArxivWebNotFoundError:
            hit = None
        else:
            hit = parse_abs_page(response.text, key)
        with self._abs_lock:
            self._abs_cache[key] = hit
        return hit

    def _size_for(self, limit: int) -> int:
        """请求条数 → 主站认可的 ``size``（向上取整到 `ALLOWED_PAGE_SIZES`）。

        直接透传调用方数字会踩 400：主站只认 25/50/100/200，其余一律 Bad Request，
        非法参数返回 400，作为 provider failure 传播。
        """

        target = max(self._page_size, limit)
        for candidate in ALLOWED_PAGE_SIZES:
            if candidate >= target:
                return candidate
        return MAX_PAGE_SIZE

    def _prime_abs_cache(self, hits: Sequence[SearchHit]) -> None:
        """用检索结果页预热 ``/abs/`` 缓存。

        结果页与详情页的 title / abstract / authors / year / doi 同源同形，而检索链
        拿到候选后**必然**再走一次元数据核验（``StructuredSourceRetrievalTool``
        的 ``_enrich_metadata``）。不预热的话每个候选都要多打一次主站 —— 8 个候选
        在 2 秒节流下串行 ≈ 17 秒；预热后这一步是 0 请求。

        用 ``setdefault``：真抓过 ``/abs/`` 的条目是更权威的版本，不允许被覆盖。
        被本地 ANDNOT 排除的命中同样入缓存 —— 排除是查询级的取舍，缓存是身份级的事实。
        """

        with self._abs_lock:
            for hit in hits:
                self._abs_cache.setdefault(hit.document_id, hit)


class ArxivWebMetadataTool(MetadataTool):
    """主站元数据核验：``/abs/`` 详情页 → 规范 EvidenceSource。

    ``search`` 传入检索链的 ``ArxivWebSearchTool`` 时复用它的 ``/abs/`` 缓存 ——
    工作流里同一个 work_id 常先被检索链解析、再被元数据核验，不共享的话
    每次都要多打一次主站（冷请求 5~25 秒）。
    """

    source_id = "arxiv"

    def __init__(
        self,
        *,
        session: ArxivWebSession | None = None,
        search: ArxivWebSearchTool | None = None,
    ) -> None:
        self._session = session or (search._session if search else None)
        self._session = self._session or ArxivWebSession()
        self._search = search or ArxivWebSearchTool(session=self._session)
        self._cache: dict[str, EvidenceSource | None] = {}
        self.last_error: str | None = None

    def resolve(self, document_id: str) -> EvidenceSource | None:
        doc_id = strip_version(document_id)
        if doc_id in self._cache:
            return self._cache[doc_id]
        try:
            hit = self._search.abs_hit(doc_id)
        except ArxivWebChannelError as exc:
            self.last_error = str(exc)
            raise
        source = (
            EvidenceSource(title=hit.title, doi=hit.doi, url=hit.url)
            if hit is not None
            else None
        )
        self._cache[doc_id] = source
        return source


def build_arxiv_web_session(
    options: Mapping[str, Any] | None = None,
    *,
    client: httpx.Client | None = None,
) -> ArxivWebSession:
    """按配置构造主站会话；检索与元数据工具应共用同一个实例。"""

    config = dict(options or {})
    # 刻意不继承 API 的 timeout_seconds：那个值是按 export API 的响应速度定的
    # （20 秒），套到 5~25 秒的搜索页上会大面积超时。
    timeout = float(
        config.get("web_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    )
    return ArxivWebSession(
        client=client,
        timeout=timeout,
        min_interval=float(
            config.get("web_min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)
        ),
        max_retries=int(config.get("web_max_retries", DEFAULT_MAX_RETRIES)),
        circuit_cooldown_seconds=float(config.get("web_circuit_cooldown_seconds", 60.0)),
        retry_budget_seconds=float(config.get("web_retry_budget_seconds", 90.0)),
        max_retry_delay=float(config.get("web_max_retry_delay_seconds", 10.0)),
        max_consecutive_failures=int(
            config.get(
                "web_max_consecutive_failures", DEFAULT_MAX_CONSECUTIVE_FAILURES
            )
        ),
    )


def build_arxiv_web_search_tool(
    options: Mapping[str, Any] | None = None,
    *,
    session: ArxivWebSession | None = None,
) -> ArxivWebSearchTool:
    """按配置构造主站检索工具，接口与 ``ArxivSearchTool`` 对齐。"""

    config = dict(options or {})
    return ArxivWebSearchTool(
        session=session or build_arxiv_web_session(config),
        page_size=int(config.get("web_page_size", DEFAULT_PAGE_SIZE)),
    )


def build_arxiv_web_metadata_tool(
    options: Mapping[str, Any] | None = None,
    *,
    session: ArxivWebSession | None = None,
    search: ArxivWebSearchTool | None = None,
) -> ArxivWebMetadataTool:
    """按配置构造主站元数据工具；传 ``search`` 可复用其 ``/abs/`` 缓存。"""

    config = dict(options or {})
    shared_session = session or (search._session if search else None)
    return ArxivWebMetadataTool(
        session=shared_session or build_arxiv_web_session(config),
        search=search,
    )
