"""Spreadsheet Engine — low-level substrate (parse/sample/transform/materialize/
synthesize) reused by ``hiveflow.spreadsheet_lab``, the sole remaining
orchestration layer for spreadsheet ingestion (see that package)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hiveflow.spreadsheet.parser import parse_workbook

__all__ = ["parse_workbook"]


def __getattr__(name: str):
    if name == "parse_workbook":
        from hiveflow.spreadsheet.parser import parse_workbook

        return parse_workbook
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
