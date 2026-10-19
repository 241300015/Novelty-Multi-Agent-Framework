"""Minimal layered arXiv access/recall smoke for ARXIV-RATE-01."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from ..tools.database_search.providers.arxiv import (
    ARXIV_QUERY_URL,
    ATOM_NS,
    ArxivSearchTool,
    parse_entry,
    strip_version,
)


RUN_D_STRUCTURED_QUERY = (
    'abs:"music structure analysis" AND abs:"similarity statistics" '
    'AND abs:"fine-grained attention"'
)


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    purpose: str
    query: str
    parameter_name: str = "search_query"
    limit: int | None = None
    expected_arxiv_id: str | None = None
    expected_title: str | None = None


DEFAULT_CASES = (
    SmokeCase(
        case_id="A",
        purpose="known_arxiv_id",
        query="1706.03762",
        parameter_name="id_list",
        limit=1,
        expected_arxiv_id="1706.03762",
    ),
    SmokeCase(
        case_id="B",
        purpose="exact_english_title",
        query='ti:"Attention Is All You Need"',
        expected_title="Attention Is All You Need",
    ),
    SmokeCase(
        case_id="C",
        purpose="run_d_structured_query",
        query=RUN_D_STRUCTURED_QUERY,
    ),
)


def _normalized_title(value: str) -> str:
    return " ".join(value.casefold().split())


def probe_case(
    tool: ArxivSearchTool,
    case: SmokeCase,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """Probe once and preserve HTTP → Atom → entry as separate gates."""

    parameters: dict[str, str | int] = {
        case.parameter_name: case.query,
        "max_results": case.limit or limit,
    }
    if case.parameter_name == "search_query":
        parameters["start"] = 0
    url = f"{tool._base_url}?{urlencode(parameters)}"
    result: dict[str, Any] = {
        "case": case.case_id,
        "purpose": case.purpose,
        "query": case.query,
        "request_parameter": case.parameter_name,
        "http_status": None,
        "http_200": False,
        "atom_parsed": False,
        "entry_count": None,
        "expected_match": None,
        "classification": "PROVIDER_ACCESS_FAILURE",
        "error_type": None,
        "error": None,
    }
    try:
        response = tool._get(url)
        result["http_status"] = response.status_code
        result["http_200"] = response.status_code == 200
        if response.status_code != 200:
            result["classification"] = "HTTP_NON_200"
            return result
        root = ET.fromstring(response.text)
        result["atom_parsed"] = True
        hits = [parse_entry(entry) for entry in root.findall(f"{ATOM_NS}entry")]
        result["entry_count"] = len(hits)
        if not hits:
            result["classification"] = "VALID_EMPTY"
            return result
        expected_match = True
        if case.expected_arxiv_id:
            expected = strip_version(case.expected_arxiv_id).casefold()
            expected_match = any(
                strip_version(hit.document_id).casefold() == expected for hit in hits
            )
        if case.expected_title:
            expected = _normalized_title(case.expected_title)
            expected_match = expected_match and any(
                _normalized_title(hit.title) == expected for hit in hits
            )
        result["expected_match"] = expected_match
        result["classification"] = (
            "VALID_HIT" if expected_match else "UNEXPECTED_ENTRY"
        )
        return result
    except httpx.HTTPStatusError as exc:
        result["http_status"] = exc.response.status_code
        result["classification"] = "HTTP_NON_200"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        return result
    except ET.ParseError as exc:
        result["classification"] = "ATOM_PARSE_FAILURE"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        return result


def run_smoke(
    *,
    tool: ArxivSearchTool | None = None,
    cases: tuple[SmokeCase, SmokeCase, SmokeCase] = DEFAULT_CASES,
) -> dict[str, Any]:
    """Run A/B/C sequentially and stop before query recall becomes ambiguous."""

    provider = tool or ArxivSearchTool(min_interval=4.0, max_retries=1)
    observations: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if index and observations[-1]["classification"] != "VALID_HIT":
            observations.extend(
                {
                    "case": skipped.case_id,
                    "purpose": skipped.purpose,
                    "query": skipped.query,
                    "request_parameter": skipped.parameter_name,
                    "classification": "SKIPPED_UPSTREAM_GATE",
                }
                for skipped in cases[index:]
            )
            break
        observations.append(probe_case(provider, case))

    by_case = {item["case"]: item for item in observations}
    access_ok = all(
        by_case[case_id]["classification"] == "VALID_HIT"
        for case_id in ("A", "B")
    )
    c_classification = by_case["C"]["classification"]
    recall_measured = access_ok and c_classification in {"VALID_HIT", "VALID_EMPTY"}
    return {
        "check": "ARXIV-RATE-01",
        "endpoint": ARXIV_QUERY_URL,
        "provider_access": "PASS" if access_ok else "FAIL",
        "structured_query_recall": (
            "HIT"
            if recall_measured and c_classification == "VALID_HIT"
            else "EMPTY"
            if recall_measured
            else "NOT_MEASURED"
        ),
        "query_layer_decision_allowed": bool(
            access_ok and c_classification == "VALID_EMPTY"
        ),
        "cases": observations,
    }
