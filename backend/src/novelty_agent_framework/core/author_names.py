"""Conservative formatting of personal names for metadata lookup and comparison."""


def author_name_versions(name: str) -> tuple[str, ...]:
    """Return given-first and comma-delimited family-first forms when possible.

    Unmarked names are assumed given-first with a single final family-name token.
    Ambiguous or malformed inputs remain unchanged.
    """
    name = " ".join(name.split())
    if name.count(",") == 1:
        family, given = (part.strip() for part in name.split(","))
        if family and given:
            return (f"{given} {family}", f"{family}, {given}")
    elif "," not in name:
        parts = name.split()
        if len(parts) >= 2:
            return (name, f"{parts[-1]}, {' '.join(parts[:-1])}")
    return (name,)


def _author_name_key(name: str) -> tuple[str, ...]:
    """Preserve an explicit surname boundary; infer only a final-token surname."""
    name = " ".join(name.split()).casefold()
    if name.count(",") == 1:
        family, given = (part.strip() for part in name.split(","))
        if family and given:
            return ("person", given, family)
    elif "," not in name:
        parts = name.split()
        if len(parts) >= 2:
            return ("person", " ".join(parts[:-1]), parts[-1])
    return ("literal", name)


def author_names_key(names) -> tuple[tuple[str, ...], ...]:
    return tuple(_author_name_key(name) for name in names)
