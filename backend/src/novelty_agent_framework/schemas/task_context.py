"""Read the former inline task context without rewriting historical artifacts."""

LEGACY_CONTEXT_MARKER = "\n补检系统上下文："


def split_task_description(description: str) -> tuple[str, str | None]:
    """Return the public description and the complete legacy context, if present."""
    description, marker, context = description.partition(LEGACY_CONTEXT_MARKER)
    return description, context if marker else None
