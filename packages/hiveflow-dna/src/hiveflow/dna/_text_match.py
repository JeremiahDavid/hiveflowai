"""Shared name/stem normalization for deterministic entity and field matching.

Used by `join_proposals.py` (spreadsheet table -> lake join proposals) and
`industry_mapping.py` (silver columns -> industry template field candidates).
Pure string functions, no I/O.
"""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "by",
        "for",
        "of",
        "one",
        "per",
        "row",
        "rows",
        "the",
        "and",
        "with",
        "each",
    }
)
KEY_SUFFIXES = ("_id", "id", "_key", "_code", "_no", "_number", "_num")
_NORM_RE = re.compile(r"[^a-z0-9]+")


def norm(name: str) -> str:
    return _NORM_RE.sub("_", str(name or "").strip().lower()).strip("_")


def tokens(text: str) -> set[str]:
    parts = {norm(part) for part in re.split(r"[^a-zA-Z0-9]+", str(text or "")) if part}
    return {part for part in parts if part and part not in STOPWORDS and len(part) > 1}


def stems(name: str) -> set[str]:
    normalized = norm(name)
    if not normalized:
        return set()
    result = {normalized}
    for suffix in KEY_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            result.add(normalized[: -len(suffix)].rstrip("_"))
    if normalized in {"id", "pk", "key"}:
        result.add("id")
    return {stem for stem in result if stem}


def column_lookup(columns: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for column in columns:
        raw = str(column or "").strip()
        if not raw:
            continue
        lookup.setdefault(norm(raw), raw)
        for stem in stems(raw):
            lookup.setdefault(stem, raw)
    return lookup
