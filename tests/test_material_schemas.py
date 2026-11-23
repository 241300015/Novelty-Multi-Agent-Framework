"""Contract fixtures and rejection cases shared by Provider and Reader branches."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from novelty_agent_framework import schemas
from novelty_agent_framework.schemas.materials import (
    MaterialAcquisitionRequest,
    MaterialAcquisitionResult,
    MaterialOpenRequest,
    MaterialOpenResult,
    MaterialReadRequest,
    MaterialReadResult,
    RawRecordBinding,
    RecordSelector,
    SourceMap,
    SourceMapping,
    canonical_material_json,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "docs/provider-reader-interface-v1.example.json"


@pytest.fixture
def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "step,request_type,result_type",
    [
        ("acquire", MaterialAcquisitionRequest, MaterialAcquisitionResult),
        ("open", MaterialOpenRequest, MaterialOpenResult),
        ("read_first", MaterialReadRequest, MaterialReadResult),
        ("read_rest", MaterialReadRequest, MaterialReadResult),
        ("read_eof", MaterialReadRequest, MaterialReadResult),
    ],
)
def test_documented_handoff_round_trips(example, step, request_type, result_type):
    for part, model in (("request", request_type), ("result", result_type)):
        payload = example[step][part]
        instance = model.model_validate(payload)
        assert instance.model_dump(mode="json") == payload
        assert model.model_validate_json(instance.model_dump_json()) == instance
        assert getattr(schemas, model.__name__) is model
        assert model.model_json_schema()["additionalProperties"] is False


def test_fixture_bytes_identity_and_read_chain(example):
    files = example["fixture_files_utf8"]
    raw = MaterialAcquisitionResult.model_validate(example["acquire"]["result"]).materials[0]
    parsed = MaterialOpenResult.model_validate(example["open"]["result"]).document
    assert parsed is not None
    for descriptor in (raw, parsed, parsed.source_map):
        body = files[descriptor.relative_path].encode("utf-8")
        digest = descriptor.sha256 if descriptor is parsed.source_map else descriptor.ref.sha256
        assert len(body) == descriptor.byte_size
        assert hashlib.sha256(body).hexdigest() == digest
    source_map = SourceMap.model_validate_json(files[parsed.source_map.relative_path])
    assert source_map.source_ref == raw.ref == parsed.source_ref
    assert source_map.parsed_ref == parsed.ref
    assert parsed.binding_sha256 == raw.bindings[0].binding_sha256
    config = example["fixture_parser_config"]
    assert hashlib.sha256(canonical_material_json(config)).hexdigest() == parsed.parser.config_sha256
    text = files[parsed.relative_path]
    assert parsed.char_count == len(text)
    for step in ("read_first", "read_rest", "read_eof"):
        request = MaterialReadRequest.model_validate(example[step]["request"])
        result = MaterialReadResult.model_validate(example[step]["result"])
        part = result.read
        assert part is not None
        assert part.parsed_ref == request.parsed_ref == parsed.ref
        assert part.text == text[part.char_start:part.char_end]
        assert part.char_start == request.char_start
        assert len(part.text) <= request.max_chars
        assert part.has_more == (part.char_end < len(text))
        identity = {
            "parsed_ref": part.parsed_ref.model_dump(mode="json"),
            "source_map_sha256": parsed.source_map.sha256,
            "char_start": part.char_start,
            "char_end": part.char_end,
        }
        assert part.read_id == "read_" + hashlib.sha256(canonical_material_json(identity)).hexdigest()[:24]
    assert example["read_first"]["result"]["read"]["text"].endswith(" ")
    assert example["read_rest"]["result"]["read"]["text"].endswith("\n")


@pytest.mark.parametrize("field,value", [
    ("format_preferences", []),
    ("format_preferences", ["html", "html"]),
    ("format_preferences", ["epub"]),
    ("requested_role", "search_response"),
    ("max_bytes", 0),
    ("max_bytes", True),
    ("schema_version", 2),
    ("schema_version", True),
    ("schema_version", 1.0),
    ("unexpected", "value"),
])
def test_invalid_acquisition_request(example, field, value):
    data = example["acquire"]["request"]
    data[field] = value
    with pytest.raises(ValidationError):
        MaterialAcquisitionRequest.model_validate(data)


@pytest.mark.parametrize("path", ["/tmp/raw.html", "../raw.html", "a/../raw.html", "C:/raw.html", "a\\raw.html", "a//raw.html", "a/./raw.html", "a/\x00raw.html"])
def test_raw_paths_cannot_escape_scope(example, path):
    data = example["acquire"]["result"]
    data["materials"][0]["relative_path"] = path
    with pytest.raises(ValidationError):
        MaterialAcquisitionResult.model_validate(data)


def test_binding_digest_pins_identity_selector_and_null(example):
    data = example["acquire"]["result"]["materials"][0]["bindings"][0]
    data["selector"] = {"kind": "html_xpath", "value": "/html/body/article[2]"}
    with pytest.raises(ValidationError, match="binding_sha256"):
        RawRecordBinding.model_validate(data)
    payload = {key: value for key, value in data.items() if key != "binding_sha256"}
    data["binding_sha256"] = hashlib.sha256(canonical_material_json(payload)).hexdigest()
    assert RawRecordBinding.model_validate(data).selector.kind == "html_xpath"


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.update(transfer_complete=False),
    lambda raw: raw.update(transfer_complete=1),
    lambda raw: raw["ref"].update(sha256="A" * 64),
    lambda raw: raw["provenance"].update(http_status=206),
    lambda raw: raw["provenance"].update(http_status=403),
    lambda raw: raw["provenance"].update(acquired_at="2026-11-15T00:00:00"),
    lambda raw: raw["bindings"].append(deepcopy(raw["bindings"][0])),
    lambda raw: raw["format"].update(family="pdf", media_type="application/pdf", charset="utf-8"),
    lambda raw: raw["format"].update(media_type="text/html; charset=utf-8"),
])
def test_raw_material_rejects_invalid_integrity_metadata(example, mutate):
    data = example["acquire"]["result"]
    mutate(data["materials"][0])
    with pytest.raises(ValidationError):
        MaterialAcquisitionResult.model_validate(data)


@pytest.mark.parametrize("status", ["failed", "unavailable", "unauthorized", "unsupported", "not_found"])
def test_unsuccessful_acquisition_cannot_return_materials(example, status):
    data = example["acquire"]["result"]
    data["status"] = status
    with pytest.raises(ValidationError):
        MaterialAcquisitionResult.model_validate(data)


def test_acquisition_success_requires_materials_and_attempt_ids_are_unique(example):
    data = example["acquire"]["result"]
    data["attempts"].append(deepcopy(data["attempts"][0]))
    with pytest.raises(ValidationError, match="attempt_id"):
        MaterialAcquisitionResult.model_validate(data)
    data["attempts"] = []  # cache hits do not invent HTTP requests
    MaterialAcquisitionResult.model_validate(data)
    data["materials"] = []
    for status in ("acquired", "partial"):
        data["status"] = status
        with pytest.raises(ValidationError):
            MaterialAcquisitionResult.model_validate(data)


@pytest.mark.parametrize("model,payload", [
    (MaterialAcquisitionResult, {"materials": [], "attempts": []}),
    (MaterialOpenResult, {"document": None}),
    (MaterialReadResult, {"read": None}),
])
def test_failed_results_need_explicit_issues(model, payload):
    with pytest.raises(ValidationError, match="issues"):
        model.model_validate({"schema_version": 1, "request_id": "req", "status": "failed", "issues": [], **payload})


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["ref"].update(artifact_id=doc["source_ref"]["artifact_id"]),
    lambda doc: doc["source_ref"].update(subject_paper_id="other-paper"),
    lambda doc: doc["source_ref"].update(namespace="subject_reference"),
    lambda doc: doc.update(char_count=0),
    lambda doc: doc.update(byte_size=1),
    lambda doc: doc.update(content_extent="partial"),
    lambda doc: doc["source_map"].update(relative_path=doc["relative_path"]),
])
def test_parsed_identity_and_coverage_cannot_be_misrepresented(example, mutate):
    data = example["open"]["result"]
    mutate(data["document"])
    with pytest.raises(ValidationError):
        MaterialOpenResult.model_validate(data)


def test_open_partial_requires_partial_document(example):
    data = example["open"]["result"]
    data["status"] = "partial"
    with pytest.raises(ValidationError):
        MaterialOpenResult.model_validate(data)
    data["document"]["content_extent"] = "partial"
    MaterialOpenResult.model_validate(data)
    data["status"] = "unsupported"
    with pytest.raises(ValidationError):
        MaterialOpenResult.model_validate(data)
    data["document"] = None
    MaterialOpenResult.model_validate(data)


@pytest.mark.parametrize("mutate", [
    lambda result: result.update(status="eof"),
    lambda result: result["read"].update(text="Graph"),
    lambda result: result["read"].update(origins=[]),
    lambda result: result["read"]["origins"][0].update(parsed_char_end=7),
    lambda result: result["read"]["origins"][0].update(parsed_char_start=6),
])
def test_read_preserves_text_and_clips_origins(example, mutate):
    data = example["read_first"]["result"]
    mutate(data)
    with pytest.raises(ValidationError):
        MaterialReadResult.model_validate(data)


def test_eof_has_no_more_content_or_origins(example):
    data = example["read_eof"]["result"]
    data["read"]["has_more"] = True
    with pytest.raises(ValidationError):
        MaterialReadResult.model_validate(data)


def test_unicode_offsets_count_codepoints_not_utf8_bytes(example):
    data = example["read_first"]["result"]
    data["read"].update(text=" 论文🙂\n ")  # six code points; preserve both ends
    part = MaterialReadResult.model_validate(data).read
    assert part is not None
    assert len(part.text) == 6
    assert len(part.text.encode("utf-8")) > 6


@pytest.mark.parametrize("locator,precision", [
    ({"kind": "document"}, "exact"),
    ({"kind": "pdf_page", "page": 1}, "exact"),
    ({"kind": "pdf_page", "page": 0}, "structural"),
    ({"kind": "text_chars", "start": 0, "end": 2, "charset": "utf-8"}, "exact"),
])
def test_mapping_precision_does_not_invent_exact_offsets(locator, precision):
    with pytest.raises(ValidationError):
        SourceMapping(parsed_char_start=0, parsed_char_end=6, precision=precision, locator=locator)


@pytest.mark.parametrize("value", ["", "/records/0", "/a~1b/~0key", "/a "])
def test_record_selector_preserves_valid_json_pointer(value):
    selector = TypeAdapter(RecordSelector).validate_python({"kind": "json_pointer", "value": value})
    assert selector.value == value


@pytest.mark.parametrize("value", ["records/0", "/a~2b", "/a~"])
def test_record_selector_rejects_invalid_json_pointer(value):
    with pytest.raises(ValidationError):
        TypeAdapter(RecordSelector).validate_python({"kind": "json_pointer", "value": value})
