"""DataCite anonymous singleton metadata lookup, limited to Findable records."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from ....ports import DoiLookupResult, DoiMetadataRecord, MetadataTool
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
        types = attributes.get("types") or {}
        if not isinstance(types, dict):
            raise DoiResponseError("DataCite types must be an object")
        abstracts = [text_field(item.get("description"))
                     for item in object_list(attributes.get("descriptions"))
                     if item.get("descriptionType") == "Abstract"]
        return self._found(requested, DoiMetadataRecord(
            source_id=self.source_id, doi=requested, record_kind="bibliographic",
            title=next((title for title in titles if title), None), authors=tuple(authors),
            year=publication_year(attributes.get("publicationYear")),
            url=landing_url(attributes.get("url")) or "https://doi.org/" + quote(requested, safe="/"),
            publisher=text_field(publisher),
            resource_type=text_field(types.get("resourceTypeGeneral")),
            abstract=next((abstract for abstract in abstracts if abstract), None), raw_metadata=payload,
        ))


def build_datacite_source(config: Mapping[str, Any]) -> RetrievalSource:
    tool = DataCiteMetadataTool(**provider_options(config)) if config.get("enabled", False) else None
    return RetrievalSource(source_id="datacite", doi_lookup_tool=tool, metadata_tool=tool)
