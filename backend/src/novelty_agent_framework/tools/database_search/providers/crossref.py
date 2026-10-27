"""Crossref singleton DOI metadata provider; no topical search or validation policy."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from ....ports import DoiLookupResult, DoiMetadataRecord, MetadataTool
from ..retrieval_sources import RetrievalSource
from .doi_common import (
    BibliographicDoiApi, DoiResponseError, json_object, landing_url, normalize_doi,
    object_list, provider_options, publication_year, text_field, verify_doi,
)


class CrossrefMetadataTool(BibliographicDoiApi, MetadataTool):
    source_id = "crossref"
    base_url = "https://api.crossref.org"
    path_prefix = "/works/"
    coverage = "crossref_metadata"

    def lookup(self, doi: str) -> DoiLookupResult:
        requested = normalize_doi(doi)
        response = self._get(requested)
        if response.status_code == 404:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if (content_type == "text/plain" and response.content.strip().lower()
                    in {b"resource not found", b"resource not found."}):
                return self._not_found(requested)
            self._http_error(response)
        payload = json_object(response)
        record = payload.get("message")
        if (payload.get("status") != "ok" or payload.get("message-type") != "work"
                or not isinstance(record, dict)):
            raise DoiResponseError("Crossref response is not a successful singleton work")
        verify_doi(record.get("DOI"), requested)
        titles = record.get("title", [])
        if not isinstance(titles, list):
            raise DoiResponseError("Crossref title must be a list")
        titles = [text_field(value) for value in titles]
        authors = []
        for author in object_list(record.get("author")):
            name = text_field(author.get("name")) or " ".join(filter(None, (
                text_field(author.get("given")), text_field(author.get("family")),
            )))
            if name:
                authors.append(name)
        return self._found(requested, DoiMetadataRecord(
            source_id=self.source_id, doi=requested, record_kind="bibliographic",
            title=next((title for title in titles if title), None), authors=tuple(authors),
            year=_published_year(record),
            url=landing_url(record.get("URL")) or "https://doi.org/" + quote(requested, safe="/"),
            publisher=text_field(record.get("publisher")), resource_type=text_field(record.get("type")),
            abstract=text_field(record.get("abstract")), raw_metadata=payload,
        ))


def _published_year(record: dict[str, Any]) -> int | None:
    for field in ("published", "published-print", "published-online", "issued"):
        date = record.get(field)
        if date is None:
            continue
        if not isinstance(date, dict):
            raise DoiResponseError("Crossref publication date must be an object")
        parts = date.get("date-parts")
        if parts is None or parts == [] or parts == [[]]:
            continue
        if not isinstance(parts, list) or not isinstance(parts[0], list) or not parts[0]:
            raise DoiResponseError("invalid Crossref date-parts")
        year = publication_year(parts[0][0])
        if year is not None:
            return year
    return None


def build_crossref_source(config: Mapping[str, Any]) -> RetrievalSource:
    tool = CrossrefMetadataTool(**provider_options(config)) if config.get("enabled", False) else None
    return RetrievalSource(source_id="crossref", doi_lookup_tool=tool, metadata_tool=tool)
