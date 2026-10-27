"""DOI.org Handle registration lookup; this API does not supply paper titles."""

from collections.abc import Mapping
from typing import Any

from ....ports import DoiLookupResult, DoiMetadataRecord
from ..retrieval_sources import RetrievalSource
from .doi_common import (
    DoiResponseError, PublicDoiApi, json_object, landing_url, normalize_doi,
    object_list, provider_options, verify_doi,
)


class DoiOrgLookupTool(PublicDoiApi):
    source_id = "doi_org"
    base_url = "https://doi.org"
    path_prefix = "/api/handles/"
    coverage = "handle_registry"

    def lookup(self, doi: str) -> DoiLookupResult:
        requested = normalize_doi(doi)
        response = self._get(requested)
        payload = json_object(response)
        verify_doi(payload.get("handle"), requested)
        code = payload.get("responseCode")
        if response.status_code == 404:
            if type(code) is int and code == 100 and not payload.get("values"):
                return self._not_found(requested)
            self._http_error(response)
        if type(code) is not int or code not in {1, 200}:
            raise DoiResponseError("DOI.org returned an unexpected Handle responseCode")
        # Code 200 means the Handle exists but values were not found (not code 100).
        values = object_list(payload.get("values"))
        if code == 1 and not values:
            raise DoiResponseError("successful Handle response has no values")
        url = None
        for value in values:
            if not isinstance(value.get("type"), str) or not isinstance(value.get("data"), dict):
                raise DoiResponseError("invalid Handle value")
            if value["type"] == "URL" and value["data"].get("format") == "string":
                candidate = landing_url(value["data"].get("value"))
                url = url or candidate
        return self._found(requested, DoiMetadataRecord(
            source_id=self.source_id, doi=requested, record_kind="handle",
            url=url, raw_metadata=payload,
        ))


def build_doi_org_source(config: Mapping[str, Any]) -> RetrievalSource:
    tool = DoiOrgLookupTool(**provider_options(config)) if config.get("enabled", False) else None
    return RetrievalSource(source_id="doi_org", doi_lookup_tool=tool)
