"""Schemas used only by legacy Researcher tool compatibility paths.

LEGACY / UNUSED by the canonical Harness. Retained for historical experiments and
regression checks; new code must use ``DatabaseSearchArguments``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from .domain import StrictModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StructuredRetrievalToolArguments(StrictModel):
    """Legacy database-bound retrieval wrapper arguments."""

    source_id: NonEmptyStr
