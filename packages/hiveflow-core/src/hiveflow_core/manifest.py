"""Cross-component data contracts and small serialization helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CHUNK = 1 << 20


def hash_file(path: str | Path) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for a file, read in chunks."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def json_default(value: Any) -> Any:
    """`default=` for ``json.dumps`` covering the types our models emit."""
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, set | frozenset):
        return sorted(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class HiveflowModel(BaseModel):
    """Base for every serialized contract in the repo."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceInfo(HiveflowModel):
    """Provenance for an input artifact processed by a component."""

    filename: str
    sha256: str
    bytes: int
    format: str
    parsed_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    parser_version: str

    @classmethod
    def for_file(cls, path: str | Path, *, fmt: str, parser_version: str) -> SourceInfo:
        p = Path(path)
        sha, size = hash_file(p)
        return cls(
            filename=p.name,
            sha256=sha,
            bytes=size,
            format=fmt,
            parser_version=parser_version,
        )
