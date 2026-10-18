"""把单篇论文的结构化查新产物确定性渲染为最终报告。"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from ..persistence import DEFAULT_OUTPUTS_DIR, paper_workspace
from ..report_inputs import ARTIFACT_FILES, ReportInputVersionError, load_verified_report_inputs
from .renderer_sections import build_stage_context

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
DEFAULT_MARKDOWN_TEMPLATE = TEMPLATES_DIR / "markdown" / "default.md"


class ReportRenderError(RuntimeError):
    """报告模板、输入产物或输出格式不合法。"""


class ReportRenderer(ABC):
    """报告 Renderer 的统一协议。"""

    output_suffix: str

    @abstractmethod
    def render(
        self,
        *,
        template_path: str | Path,
        paper_name: str,
        save_path: str | Path,
        output_root: str | Path = DEFAULT_OUTPUTS_DIR,
        expected_run_id: str | None = None,
    ) -> Path:
        """读取 paper 工作目录并将报告写入 save_path。"""


class MarkdownRenderer(ReportRenderer):
    """使用 Mustache 风格简单占位符生成 Markdown 报告。"""

    output_suffix = ".md"

    def render(
        self,
        *,
        template_path: str | Path,
        paper_name: str,
        save_path: str | Path,
        output_root: str | Path = DEFAULT_OUTPUTS_DIR,
        expected_run_id: str | None = None,
    ) -> Path:
        template = Path(template_path)
        if not template.is_file():
            raise ReportRenderError(f"Markdown 模板不存在：{template}")

        workspace = paper_workspace(paper_name, output_root=output_root, create=False)
        data = _load_workspace_data(workspace, expected_run_id=expected_run_id)
        destination = Path(save_path)
        if destination.suffix.lower() != self.output_suffix:
            destination = destination.with_suffix(self.output_suffix)
        context = _build_markdown_context(paper_name, data)
        context.update(build_stage_context(data, workspace, destination))
        rendered = _render_template(template.read_text(encoding="utf-8"), context)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered.rstrip() + "\n", encoding="utf-8", newline="\n")
        return destination


class RendererFactory:
    """按格式选择 Renderer，便于后续注册 LaTeX、HTML 实现。"""

    _renderers: dict[str, type[ReportRenderer]] = {
        "md": MarkdownRenderer,
        "markdown": MarkdownRenderer,
    }

    @classmethod
    def create(cls, output_format: str) -> ReportRenderer:
        normalized = output_format.strip().lower()
        renderer_type = cls._renderers.get(normalized)
        if renderer_type is None:
            supported = ", ".join(sorted(cls._renderers))
            raise ReportRenderError(
                f"不支持的报告格式：{output_format!r}；当前支持：{supported}"
            )
        return renderer_type()

    @classmethod
    def register(cls, output_format: str, renderer: type[ReportRenderer]) -> None:
        """注册后续输出格式实现。"""

        cls._renderers[output_format.strip().lower()] = renderer


def render_report(
    output_format: str = "markdown",
    template_path: str | Path | None = None,
    paper_name: str = "",
    save_path: str | Path | None = None,
    output_root: str | Path = DEFAULT_OUTPUTS_DIR,
    expected_run_id: str | None = None,
) -> Path:
    """统一报告渲染入口。

    默认读取 ``outputs/<paper_name>/``，使用内置 Markdown 模板，并写入
    ``outputs/<paper_name>/report/<paper_name>-report.md``。
    """

    if not paper_name.strip():
        raise ReportRenderError("paper_name 不能为空")
    renderer = RendererFactory.create(output_format)
    workspace = paper_workspace(paper_name, output_root=output_root, create=False)
    template = Path(template_path) if template_path else _default_template(renderer)
    destination = (
        Path(save_path)
        if save_path is not None
        else workspace / "report" / f"{workspace.name}-report{renderer.output_suffix}"
    )
    return renderer.render(
        template_path=template,
        paper_name=paper_name,
        save_path=destination,
        output_root=output_root,
        expected_run_id=expected_run_id,
    )


def _default_template(renderer: ReportRenderer) -> Path:
    if isinstance(renderer, MarkdownRenderer):
        return DEFAULT_MARKDOWN_TEMPLATE
    raise ReportRenderError(f"{type(renderer).__name__} 未配置默认模板")


def _load_workspace_data(workspace: Path, *, expected_run_id: str | None = None) -> dict[str, Any]:
    required = {name: workspace / relative for name, relative, _, required in ARTIFACT_FILES
                if required}
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise ReportRenderError("缺少报告输入产物：" + "；".join(missing))
    try:
        version, verified = load_verified_report_inputs(workspace)
        if expected_run_id is not None and version["run_id"] != expected_run_id:
            raise ReportInputVersionError("报告输入与调用方要求的运行 ID 不一致")
        data = {}
        for name, relative, _, _ in ARTIFACT_FILES:
            path = workspace / relative
            if relative not in verified:
                data[name] = {}
                continue
            value = json.loads(verified[relative].decode("utf-8"))
            if not isinstance(value, dict):
                raise ReportRenderError(f"报告输入产物应为 JSON 对象：{path}")
            data[name] = value
        paper_id = data["paper"].get("paper_id")
        if version["paper_id"] != paper_id:
            raise ReportRenderError("报告版本记录与输入论文身份不一致")
        for name, value in data.items():
            identity = value.get("paper_id", value.get("subject_paper_id"))
            if identity is not None and identity != paper_id:
                raise ReportRenderError(f"报告输入产物论文身份不一致：{name}")
        data["version"] = version
        return data
    except ReportInputVersionError as exc:
        raise ReportRenderError(f"报告输入版本门控未通过：{exc}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportRenderError(f"读取报告输入产物失败：{exc}") from exc


def _build_markdown_context(
    paper_name: str,
    data: Mapping[str, Any],
) -> dict[str, str]:
    paper = data["paper"]
    points = data["points"].get("novelty_points", [])
    plans = data["plans"].get("novelty_point_plans", [])
    evidence = data["evidence"]
    report = data["report"]
    references = data.get("references", {})
    accepted = [card for card in evidence.get("accepted_evidence_cards", [])
                if not _is_web_card(card, references)]
    rejected = evidence.get("rejected_evidence", [])

    point_by_id = {point.get("point_id", ""): point for point in points}
    accepted_by_point: dict[str, list[Mapping[str, Any]]] = {}
    for card in accepted:
        accepted_by_point.setdefault(card.get("novelty_point_id", ""), []).append(card)

    all_queries = _unique(
        query
        for plan in plans
        for query in plan.get("query_plan", {}).get("queries", [])
    )
    sources = _evidence_sources(accepted)
    purpose = paper.get("abstract") or "基于论文技术内容开展公开文献检索与新颖性对比。"
    technical_summary = _technical_summary(paper, points)
    scope_terms = _unique(
        [*paper.get("keywords_zh", []), *paper.get("keywords_en", [])]
    )
    return {
        "paper_name": _cell(paper.get("title") or paper_name),
        "paper_name_en": _cell(paper.get("metadata", {}).get("title_en") or "—"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "materials_time": " 至 ".join(
            datetime.fromisoformat(data["version"][key]).isoformat(timespec="seconds")
            for key in ("started_at", "materials_as_of")
        ),
        "novelty_scope": "、".join(scope_terms) or "论文相关技术领域",
        "purpose": purpose,
        "technical_summary": technical_summary,
        "novelty_points": _format_points(points),
        "search_scope": "检索范围围绕各查新点的中英文表述及技术特征展开。",
        "search_sources": "\n".join(f"- {source}" for source in sources)
        or "- 未记录有效证据来源",
        "search_terms": "\n".join(f"- {term}" for term in scope_terms)
        or "- 未提供关键词",
        "search_queries": _format_query_plans(plans),
        "search_summary": (
            f"记录 {data['plans'].get('rounds', 0)} 轮检索计划，"
            f"生成 {len(evidence.get('raw_evidence_cards', []))} 张原始证据卡；"
            f"通过 {len(accepted)} 张，拒绝 {len(rejected)} 张。"
        ),
        "evidence_list": _format_evidence(accepted),
        "final_evidence_counts": _format_final_evidence_counts(
            points, accepted_by_point
        ),
        "novelty_conclusions": _format_conclusions(
            report.get("conclusions", []), point_by_id
        ),
        "limitations": _format_list(report.get("limitations", []))
        + _web_advice(references, evidence.get("raw_evidence_cards", []),
                      data.get("candidate_audit", {}).get("tasks", [])),
        "attachments": _format_attachments(report, rejected)
        + "\n\n### 检索到的文献\n\n"
        + _format_retrieved_references(
            data.get("references", {}), evidence.get("raw_evidence_cards", accepted)
        ),
    }


def _render_template(template: str, context: Mapping[str, str]) -> str:
    pattern = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
    unknown = sorted({name for name in pattern.findall(template) if name not in context})
    if unknown:
        raise ReportRenderError("模板包含未知变量：" + ", ".join(unknown))
    return pattern.sub(lambda match: context[match.group(1)], template)


def _technical_summary(paper: Mapping[str, Any], points: list[Mapping[str, Any]]) -> str:
    contributions = paper.get("claimed_contributions", [])
    if contributions:
        return "\n".join(f"- {item}" for item in contributions)
    if points:
        return "\n".join(f"- {point.get('claim', '—')}" for point in points)
    return paper.get("abstract") or "未提供。"


def _format_points(points: list[Mapping[str, Any]]) -> str:
    if not points:
        return "未生成查新点。"
    rows = ["| 序号 | 中文查新点 | 英文查新点 |", "| --- | --- | --- |"]
    for point in points:
        rows.append(
            "| {id} | {claim} | {claim_en} |".format(
                id=_cell(point.get("point_id", "—")),
                claim=_cell(point.get("claim", "—")),
                claim_en=_cell(point.get("claim_en") or "—"),
            )
        )
    return "\n".join(rows)


def _format_query_plans(plans: list[Mapping[str, Any]]) -> str:
    if not plans:
        return "未生成检索计划。"
    sections = []
    for plan in plans:
        queries = plan.get("query_plan", {}).get("queries", [])
        executions = plan.get("executed_queries", [])
        if executions:
            labels = {"zero_hits": "零命中", "has_hits": "有命中", "failed": "执行失败",
                      "partial": "部分成功", "requires_human": "需人工处理"}
            body = "\n".join(
                f"  - `{item.get('query', '')}`（{item.get('database', '未知来源')}；"
                f"{labels.get(item.get('result_marker'), item.get('status', '状态未知'))}）"
                for item in executions
            )
        else:
            body = "\n".join(f"  - `{query}`" for query in queries) or "  - 无"
        sections.append(f"- **{plan.get('novelty_point_id', '未知查新点')}**\n{body}")
    return "\n".join(sections)


def _format_evidence(cards: list[Mapping[str, Any]]) -> str:
    if not cards:
        return "没有证据卡通过质量校验。"
    sections = []
    for card in cards:
        source_lines = []
        for source in card.get("sources", []):
            target = source.get("doi") or source.get("url") or source.get("location") or "—"
            source_lines.append(f"  - {source.get('title', '来源')}：{target}")
            if source.get("quote"):
                source_lines.append(f"    - 引文：{_cell(source['quote'])}")
            if source.get("location"):
                source_lines.append(f"    - 位置：{_cell(source['location'])}")
        sources = "\n".join(source_lines) or "  - 未记录"
        sections.append(
            f"### {card.get('card_id', 'Evidence')} · {card.get('document_title', '未命名文献')}\n\n"
            f"- 查新点：{card.get('novelty_point_id', '—')}\n"
            f"- 主要贡献：{card.get('main_contribution', '—')}\n"
            f"- 相关性：{card.get('relevance', 0):.2f}\n"
            f"- 置信度：{card.get('confidence', 0):.2f}\n"
            f"- 来源：\n{sources}"
        )
    return "\n\n".join(sections)


def _format_final_evidence_counts(
    points: list[Mapping[str, Any]],
    accepted_by_point: Mapping[str, list[Mapping[str, Any]]],
) -> str:
    rows = ["| 查新点 | 有效证据数 | 状态 |", "| --- | ---: | --- |"]
    for point in points:
        point_id = point.get("point_id", "—")
        count = len(accepted_by_point.get(point_id, []))
        rows.append(f"| {_cell(point_id)} | {count} | {'有证据' if count else '0 Card'} |")
    return "\n".join(rows) if points else "无查新点最终证据数据。"


def _format_conclusions(
    conclusions: list[Mapping[str, Any]],
    point_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    if not conclusions:
        return "未生成结构化查新结论。"
    labels = {
        "novel": "新颖",
        "partially_novel": "部分新颖",
        "not_novel": "不新颖",
    }
    sections = []
    for conclusion in conclusions:
        point_id = conclusion.get("novelty_point_id", "—")
        claim = point_by_id.get(point_id, {}).get("claim", "")
        status = conclusion.get("review_status")
        verdict = conclusion.get("verdict")
        label = (
            ("核验未完成，无法裁定" if conclusion.get("incomplete_reason") in {
                "budget_exhausted", "technical_error", "material_unavailable"}
             else "证据不足，无法裁定")
            if status == "insufficient_evidence"
            else labels.get(verdict, verdict or "—")
        )
        confidence = conclusion.get("confidence")
        confidence_text = "—" if confidence is None else f"{confidence:.2f}"
        relevant = conclusion.get("highly_relevant_works", [])
        relevant_text = "\n".join(
            f"  - {item.get('work_id', '—')}：{item.get('relevance_reason', '—')} "
            f"(cards: {', '.join(item.get('card_ids', [])) or '—'})"
            for item in relevant
        ) or "  - 无"
        verification_text = "\n".join(
            f"  - {item.get('evidence_id', '—')} · {item.get('work_id', '—')} · "
            f"{item.get('artifact_id', '—')} [{item.get('char_start', 0)}, "
            f"{item.get('char_end', 0)}): {item.get('exact_quote', '')}"
            for item in conclusion.get("review_evidence", [])
        ) or "  - 无"
        sections.append(
            f"### {point_id} · {label}\n\n"
            f"{claim}\n\n"
            f"**Reviewer 裁定：** {label}  \n"
            f"**裁定理由：** {conclusion.get('verdict_reason') or '—'}  \n"
            f"**置信度：** {confidence_text}  \n"
            f"**报告摘要：** {conclusion.get('summary', '—')}  \n"
            f"**未完成原因：** {conclusion.get('incomplete_reason') or '—'}  \n"
            f"**高度相关 Work：**\n{relevant_text}\n\n"
            f"**原文核验证据：**\n{verification_text}"
        )
    return "\n\n".join(sections)


def _format_attachments(report: Mapping[str, Any], rejected: list[Mapping[str, Any]]) -> str:
    parts = [
        ("缺失参考文献", report.get("missing_references", [])),
        ("缺失 Baseline", report.get("missing_baselines", [])),
        ("引用问题", report.get("citation_issues", [])),
        (
            "被拒绝证据",
            [f"{item.get('card_id', '—')}：{item.get('reason', '—')}" for item in rejected],
        ),
    ]
    sections = []
    for title, items in parts:
        content = _format_list(items) if items else "本节点输入未记录该类条目；不代表已核实为无。"
        sections.append(f"### {title}\n\n{content}")
    return "\n\n".join(sections)


def _format_list(items: list[Any]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "无。"


def _is_web_record(record: Mapping[str, Any]) -> bool:
    return (record.get("source_kind") in {"web", "web_supplement"}
            or record.get("provenance", {}).get("tool") == "web_search")


def _is_web_card(card: Mapping[str, Any], manifest: Mapping[str, Any]) -> bool:
    if card.get("provenance", {}).get("source_kind") in {"web", "web_supplement"}:
        return True
    web_urls = {url for record in manifest.get("source_records", []) if _is_web_record(record)
                for url in (record.get("landing_url"), record.get("full_text_url")) if url}
    urls = [source.get("url") for source in card.get("sources", []) if source.get("url")]
    return bool(urls) and all(url in web_urls for url in urls)


def _web_advice(manifest: Mapping[str, Any], cards: list[Mapping[str, Any]],
                tasks: list[Mapping[str, Any]] = ()) -> str:
    records = manifest.get("source_records", [])
    if any(candidate.get("namespace") == "subject_reference"
           for task in tasks for candidate in task.get("candidates", [])):
        return ""
    if any(work.get("work_type") != "webpage" for work in manifest.get("works", [])):
        return ""
    # No card is not equivalent to no paper: discovered database candidates count.
    if any(not _is_web_record(record) for record in records):
        return ""
    if any(not _is_web_card(card, manifest) for card in cards):
        return ""
    web = [record for record in records if _is_web_record(record)]
    if not web:
        return ""
    queries = _unique(record.get("provenance", {}).get("query") for record in web)[:3]
    topics = "；".join(_cell(" ".join(str(query).split())) for query in queries)
    advice = (f"可围绕这些网页检索方向调整论文检索词：{topics}。" if topics else
              "建议利用网页补充资料中的术语线索调整论文检索词，再到学术数据库核验。")
    return ("\n\n### 无论文可用时的补充信息建议\n\n"
            "本次未取得论文检索结果；这不证明相关论文不存在。" + advice +
            "网页内容仅作后续检索建议，不作为相关文献、原始证据或查新结论依据。")


def _format_retrieved_references(
    manifest: Mapping[str, Any], cards: list[Mapping[str, Any]]
) -> str:
    """List discovered sources, including candidates without accepted evidence."""
    candidates = [
        (record.get("title"), record.get("full_text_url") or record.get("landing_url"))
        for record in manifest.get("source_records", [])
        if not _is_web_record(record)
    ]
    candidates.extend(
        (card.get("document_title"), source.get("url") or (
            "https://doi.org/" + source["doi"] if source.get("doi") else None
        ))
        for card in cards if not _is_web_card(card, manifest)
        for source in card.get("sources", [])
    )
    rows = []
    seen = set()
    for title, url in candidates:
        if not title or not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if parsed.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?", parsed.path)
            if match:
                url = "https://arxiv.org/pdf/" + match.group(1)
        if url in seen:
            continue
        seen.add(url)
        # Keep titles and URLs on one line and preserve valid Markdown links.
        title = re.sub(r"([\\`*_\[\]])", r"\\\1", " ".join(str(title).split()))
        url = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        url = url.replace("[", "%5B").replace("]", "%5D").replace("\n", "%0A").replace("\r", "%0D")
        rows.append(f"- {title}：[{url}]({url})")
    return "\n".join(rows) or "未记录带可用链接的检索文献。"


def _evidence_sources(cards: list[Mapping[str, Any]]) -> list[str]:
    sources = []
    for card in cards:
        for source in card.get("sources", []):
            url = source.get("url") or ""
            host = urlparse(url).netloc
            sources.append(host or source.get("title") or "未知来源")
    return _unique(sources)


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")
