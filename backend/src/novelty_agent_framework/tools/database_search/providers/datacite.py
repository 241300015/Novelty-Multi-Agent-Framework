"""DataCite anonymous singleton metadata lookup, limited to Findable records."""

from collections.abc import Mapping
from typing import Any

from ....ports import DoiLookupResult, DoiMetadataRecord, MetadataTool, MetadataQuery, ProviderMetadataResult
from ..retrieval_sources import RetrievalSource
from .doi_common import (
    BibliographicDoiApi, DoiResponseError, json_object, landing_url, normalize_doi,
    object_list, provider_options, publication_year, text_field, verify_doi,
)


class DataCiteMetadataTool(BibliographicDoiApi, MetadataTool):
    source_id = "datacite"
    base_url = "https://api.datacite.org"
    path_prefix = "/dois/"
    coverage = "datacite_findable"

    def lookup(self, doi: str) -> DoiLookupResult:
        requested = normalize_doi(doi)
        response = self._get(requested)
        payload = json_object(response)
        if response.status_code == 404:
            errors = object_list(payload.get("errors"))
            if errors and payload.get("data") is None and all(
                str(error.get("status")) == "404"
                and isinstance(error.get("title"), str)
                and error["title"].lower() in {"not found", "the resource you are looking for doesn't exist."}
                for error in errors
            ):
                return self._not_found(requested)
            self._http_error(response)
        record = payload.get("data")
        if (payload.get("errors") or not isinstance(record, dict)
                or record.get("type") != "dois" or not isinstance(record.get("attributes"), dict)):
            raise DoiResponseError("DataCite response is not a singleton DOI")
        verify_doi(record.get("id"), requested)
        attributes = record["attributes"]
        verify_doi(attributes.get("doi"), requested)
        return self._found(requested, _parse_record(record, raw_payload=payload))

    def _search_metadata(self, query: MetadataQuery) -> ProviderMetadataResult:
        terms = []
        if query.title:
            terms.append("titles.title:" + _quote(query.title))
        terms.extend("creators.name:" + _quote(author) for author in query.authors)
        if query.year:
            terms.append(f"publicationYear:{query.year}")
        if query.citation:
            terms.append(_quote(query.citation))
        if query.venue:
            terms.append(_quote(query.venue))
        response = self._request("/dois", params={"query": " AND ".join(terms), "page[size]": query.limit})
        if response.status_code != 200:
            self._http_error(response)
        payload = json_object(response)
        if payload.get("errors") or not isinstance(payload.get("data"), list):
            raise DoiResponseError("DataCite response is not a DOI list")
        items = object_list(payload["data"])
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise DoiResponseError("DataCite meta must be an object")
        total = meta.get("total")
        if total is not None and (type(total) is not int or total < len(items)):
            raise DoiResponseError("invalid DataCite total")
        records = tuple(_parse_record(item) for item in items[:query.limit])
        return ProviderMetadataResult(
            self.source_id, query, "found" if records else "not_found", self.coverage,
            records, http_status=200,
            truncated=(total > len(records) if total is not None else len(items) >= query.limit),
        )


def _quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _parse_record(record: dict[str, Any], *, raw_payload=None) -> DoiMetadataRecord:
    if record.get("type") != "dois" or not isinstance(record.get("attributes"), dict):
        raise DoiResponseError("invalid DataCite DOI record")
    attributes = record["attributes"]
    try:
        doi = normalize_doi(record.get("id") or attributes.get("doi")) if (record.get("id") or attributes.get("doi")) else None
    except ValueError:
        raise DoiResponseError("invalid DataCite DOI") from None
    for value in (record.get("id"), attributes.get("doi")):
        if value is not None:
            verify_doi(value, doi)
    if attributes.get("state", "findable") != "findable":
        raise DoiResponseError("unexpected non-Findable DOI from DataCite public API")
    titles = [text_field(item.get("title")) for item in object_list(attributes.get("titles"))]
    authors = []
    for creator in object_list(attributes.get("creators")):
        name = text_field(creator.get("name")) or " ".join(filter(None, (
            text_field(creator.get("givenName")), text_field(creator.get("familyName")),
        )))
        if name:
            authors.append(name)
    publisher = attributes.get("publisher")
    if isinstance(publisher, dict):
        publisher = publisher.get("name")
    container = attributes.get("container") or {}
    if not isinstance(container, dict):
        raise DoiResponseError("DataCite container must be an object")
    types = attributes.get("types") or {}
    if not isinstance(types, dict):
        raise DoiResponseError("DataCite types must be an object")
    abstracts = [text_field(item.get("description"))
                 for item in object_list(attributes.get("descriptions"))
                 if item.get("descriptionType") == "Abstract"]
    return DoiMetadataRecord(
        source_id="datacite", doi=doi, record_kind="bibliographic",
        title=next((title for title in titles if title), None), authors=tuple(authors),
        year=publication_year(attributes.get("publicationYear")),
        url=landing_url(attributes.get("url")),
        publisher=text_field(publisher),
        resource_type=text_field(types.get("resourceTypeGeneral")),
        abstract=next((abstract for abstract in abstracts if abstract), None), raw_metadata=raw_payload if raw_payload is not None else record,
        venue=text_field(container.get("title")),
    )


def build_datacite_source(config: Mapping[str, Any]) -> RetrievalSource:
    tool = DataCiteMetadataTool(**provider_options(config)) if config.get("enabled", False) else None
    return RetrievalSource(source_id="datacite", doi_lookup_tool=tool, metadata_tool=tool, metadata_retrieval_tool=tool)
