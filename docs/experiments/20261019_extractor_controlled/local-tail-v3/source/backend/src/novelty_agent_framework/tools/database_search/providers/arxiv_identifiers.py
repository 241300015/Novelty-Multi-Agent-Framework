"""Shared Atom identifier extraction for search and metadata batch matching."""
from urllib.parse import urlsplit


def atom_identifier(value: str) -> str:
    """Retain the category in legacy IDs, and retain any version suffix."""
    path = urlsplit(value.strip()).path
    return path.removeprefix("/abs/")
