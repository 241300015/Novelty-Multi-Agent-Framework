"""Crossref exact DOI and bibliographic candidate retrieval; no authenticity policy."""

from collections.abc import Mapping
from typing import Any

from ....core.author_names import author_name_versions
from ....ports import DoiLookupResult, DoiMetadataRecord, MetadataTool, MetadataQuery, ProviderMetadataResult
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
        return self._found(requested, _parse_record(record, payload))

    def _search_metadata(self, query: MetadataQuery) -> ProviderMetadataResult:
        params = {"rows": query.limit}
        bibliographic = " ".join(value for value in (query.title, query.citation) if value)
        if bibliographic:
            params["query.bibliographic"] = bibliographic
        if query.authors:
            params["query.author"] = " ".join(author_name_versions(author)[0] for author in query.authors)
        if query.venue:
            params["query.container-title"] = query.venue
        if query.year:
            params["filter"] = f"from-pub-date:{query.year:04d}-01-01,until-pub-date:{query.year:04d}-12-31"
        response = self._request("/works", params=params)
        if response.status_code != 200:
            self._http_error(response)
        payload = json_object(response)
        message = payload.get("message")
        if (payload.get("status") != "ok" or payload.get("message-type") != "work-list"
                or not isinstance(message, dict) or not isinstance(message.get("items"), list)):
            raise DoiResponseError("Crossref response is not a work list")
        items = object_list(message["items"])
        total = message.get("total-results")
        if total is not None and (type(total) is not int or total < len(items)):
            raise DoiResponseError("invalid Crossref total-results")
        records = tuple(_parse_record(item, item) for item in items[:query.limit])
        return ProviderMetadataResult(
            self.source_id, query, "found" if records else "not_found", self.coverage,
            records, http_status=200,
            truncated=(total > len(records) if total is not None else len(items) >= query.limit),
        )


def _parse_record(record: dict[str, Any], payload: dict[str, Any]) -> DoiMetadataRecord:
    raw_doi = record.get("DOI")
    try:
        doi = normalize_doi(raw_doi) if raw_doi else None
    except ValueError:
        raise DoiResponseError("invalid Crossref DOI") from None
    titles = record.get("title")
    if titles is None:
        titles = []
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
    return DoiMetadataRecord(
        source_id="crossref", doi=doi, record_kind="bibliographic",
        title=next((title for title in titles if title), None), authors=tuple(authors),
        year=_published_year(record),
        url=landing_url(record.get("URL")),
        publisher=text_field(record.get("publisher")), resource_type=text_field(record.get("type")),
        abstract=text_field(record.get("abstract")), raw_metadata=payload,
        venue=_first_title(record.get("container-title")),
        volume=text_field(record.get("volume")), issue=text_field(record.get("issue")),
        pages=text_field(record.get("page")),
        page_count=record.get("page-count") if type(record.get("page-count")) is int
            and record["page-count"] > 0 else None,
    )


def _first_title(values):
    if values is None:
        return None
    if not isinstance(values, list):
        raise DoiResponseError("Crossref container-title must be a list")
    titles = [text_field(value) for value in values]
    return next((title for title in titles if title), None)


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
    return RetrievalSource(source_id="crossref", doi_lookup_tool=tool, metadata_tool=tool, metadata_retrieval_tool=tool)
