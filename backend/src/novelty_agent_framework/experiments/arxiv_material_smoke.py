"""Live abstract JSON + raw HTML/PDF acquisition smoke; no Reader or LLM calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..material_store import MaterialStore
from ..persistence import ReferenceStore
from ..schemas import AccessStatus
from ..schemas.materials import MaterialAcquisitionRequest
from ..tools.database_search.providers.arxiv_materials import ArxivMaterialProvider
from ..tools.database_search.providers.arxiv_web import ArxivWebSession, parse_abs_page
from ..tools.database_search.structured_retrieval import StructuredRetrievalAdapter


async def run(identifier: str, output_root: Path, formats: list[str]) -> dict:
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]*/\d{7})(?:v[1-9]\d*)?", identifier):
        raise ValueError("invalid arXiv identifier")
    store = MaterialStore(ReferenceStore(output_root))
    session = ArxivWebSession()
    provider = ArxivMaterialProvider(store, session=session)
    run_id = "acquisition-smoke-" + uuid.uuid4().hex
    version = re.search(r"v[1-9]\d*$", identifier)
    base = identifier[:version.start()] if version else identifier
    paper_id = "arxiv-material-smoke"
    # Use the existing abstract-page parser to discover actual metadata/abstract.
    # This probe does not perform independent bibliographic authenticity validation.
    response = await asyncio.to_thread(session.get, f"https://arxiv.org/abs/{identifier}", max_bytes=2*1024*1024)
    hit = parse_abs_page(response.text, base)
    if hit is None or not hit.abstract:
        raise ValueError("arXiv abstract page did not provide a readable abstract")
    if version and hit.external_id != identifier:
        raise ValueError("abstract page did not confirm the requested paper version")
    work, record, warnings = StructuredRetrievalAdapter().adapt_hit(hit, "arxiv", datetime.now(timezone.utc))
    store.references.merge_manifest(paper_id, works=[work], source_records=[record])
    observed_version = re.search(r"v[1-9]\d*$", record.external_id)
    request = MaterialAcquisitionRequest(
        schema_version=1, request_id=run_id + "-acquire", run_id=run_id,
        scope={"subject_paper_id": paper_id, "namespace": "research_reference"},
        source_id="arxiv", target={"work_id": work.work_id, "source_record_id": record.source_record_id,
            "external_id": record.external_id,
            "requested_version": observed_version.group() if observed_version else None},
        requested_role="full_text", format_preferences=formats, max_bytes=32*1024*1024,
    )
    parsed_before = store.load(request.scope).parsed
    acquired = await provider.acquire(request)
    report = {"request": request.model_dump(mode="json"), "acquire": acquired.model_dump(mode="json"),
              "source_record": record.model_dump(mode="json"), "warnings": warnings, "verified": False}
    if acquired.materials:
        raw = acquired.materials[0]
        artifact = store.raw_catalog_artifact(raw.ref, record.source_record_id)
        record = record.model_copy(update={"access_status": AccessStatus.FULL_TEXT_ACQUIRED})
        manifest = store.references.merge_manifest(paper_id, source_records=[record], artifacts=[artifact])
        saved = next(r for r in manifest.source_records if r.source_record_id == record.source_record_id)
        store.raw(raw.ref)  # verify registered path, complete bytes and SHA-256
        unchanged_parsed = store.load(request.scope).parsed == parsed_before
        report.update(source_record=saved.model_dump(mode="json"),
                      abstract_chars=len(saved.abstract or ""), parsed_unchanged=unchanged_parsed,
                      raw_bytes=raw.byte_size, format=raw.format.family.value)
        report["verified"] = saved.abstract == hit.abstract and unchanged_parsed
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"{run_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verified": report["verified"], "acquire": acquired.status,
                      "abstract_chars": report.get("abstract_chars"), "format": report.get("format"),
                      "raw_bytes": report.get("raw_bytes"), "report": str(path.resolve())}, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arxiv-id", default="2402.03300v1")
    parser.add_argument("--output-root", type=Path, default=Path("outputs/arxiv-acquisition-smoke"))
    parser.add_argument("--format-preferences", nargs="+", choices=["html", "pdf"], default=["html", "pdf"])
    args = parser.parse_args()
    report = asyncio.run(run(args.arxiv_id, args.output_root, args.format_preferences))
    if not report["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
