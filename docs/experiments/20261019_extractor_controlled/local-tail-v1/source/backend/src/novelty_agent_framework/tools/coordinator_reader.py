"""Reader adapter for the verified business-record index supplied to Coordinator."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from pydantic import Field

from ..schemas import ResearcherToolObservation, StrictModel
from ..schemas.failures import CodedFailure, FailureCode as C
from ..schemas.supplement_planning import SupplementPlanningInput


class CoordinatorReadArguments(StrictModel):
    material_id: str = Field(pattern=r"^M[1-9][0-9]*$")
    content_pointer: str = Field(default="", pattern=r"^(?:/(?:[^~/]|~[01])*)*$")
    char_start: int = Field(default=0, ge=0)
    max_chars: int = Field(default=4000, ge=1, le=8000)


def _select(value, pointer):
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdigit():
                raise ValueError("array pointer must be a non-negative index")
            value = value[int(token)]
        else:
            value = value[token]
    return value


class CoordinatorReaderTool:
    name = "reader"
    description = (
        "读取本次 material_index 中的完整材料，仅接受已授权 material_id。"
        "content_pointer 可选择该记录内的字段，例如论文 /full_text；空字符串读取整条记录。"
        "使用 char_start / max_chars 分段阅读；只读，不检索、不下载、不修改材料。"
    )
    args_schema = CoordinatorReadArguments

    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root).resolve()

    async def ainvoke(self, arguments: CoordinatorReadArguments, *, scope: SupplementPlanningInput):
        ref = next((r for r in scope.material_index if r.material_id == arguments.material_id), None)
        if ref is None:
            raise CodedFailure(C.TOOL_SCOPE_DENIED, "material_id is outside this planning input")
        path = (self.output_root / ref.path).resolve()
        if not path.is_relative_to(self.output_root):
            raise CodedFailure(C.READER_PATH_ESCAPE, "material path escaped output_root")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise CodedFailure(C.READER_HASH_MISMATCH, "material changed since the planning index was built")
        try:
            record = _select(json.loads(raw), ref.json_pointer)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CodedFailure(C.READER_VERIFICATION_FAILED, "indexed record cannot be resolved") from exc
        try:
            selected = _select(record, arguments.content_pointer)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CodedFailure(C.TOOL_ARGUMENT_SCHEMA, "content_pointer does not resolve within the authorized record") from exc
        text = selected if isinstance(selected, str) else json.dumps(selected, ensure_ascii=False, indent=2)
        start = min(arguments.char_start, len(text))
        end = min(start + arguments.max_chars, len(text))
        read = {"read_id": f"CR-{uuid.uuid4().hex}", "material_id": ref.material_id,
                "kind": ref.kind, "point_id": ref.point_id, "path": ref.path,
                "json_pointer": ref.json_pointer + arguments.content_pointer, "sha256": ref.sha256,
                "char_start": start, "char_end": end, "text": text[start:end], "has_more": end < len(text)}
        return ResearcherToolObservation(tool_name=self.name, arguments=arguments.model_dump(),
            succeeded=True, summary=f"读取 {ref.material_id} 字符 [{start}, {end})",
            payload={"read_result": read, "read_status": "content" if end > start else "material_eof"})

    def project_model_context(self, observation):
        return {"succeeded": observation.succeeded, **observation.payload}
