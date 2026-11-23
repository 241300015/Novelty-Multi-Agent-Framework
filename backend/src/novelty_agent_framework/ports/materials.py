"""Shared raw acquisition and offline parsing ports; DTOs live in schemas."""
from typing import Protocol

from ..schemas.materials import (
    MaterialAcquisitionRequest, MaterialAcquisitionResult, MaterialOpenRequest,
    MaterialOpenResult, MaterialReadRequest, MaterialReadResult,
)


class MaterialProvider(Protocol):
    source_id: str

    async def acquire(self, request: MaterialAcquisitionRequest) -> MaterialAcquisitionResult: ...


class MaterialReader(Protocol):
    async def open(self, request: MaterialOpenRequest) -> MaterialOpenResult: ...

    async def read(self, request: MaterialReadRequest) -> MaterialReadResult: ...
