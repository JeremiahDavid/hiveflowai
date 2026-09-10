"""Assemble a `DraftManifest` from the deterministic detect/extract/profile passes.

The agent drives this a region at a time through its tools; `build_draft` runs the
whole thing with no model in the loop (used for tests and as an offline fallback).
"""

from __future__ import annotations

from hiveflow_core import SourceInfo
from hiveflow_spreadsheet_parser import __version__
from hiveflow_spreadsheet_parser.detect import Region, detect_sheet
from hiveflow_spreadsheet_parser.extract import ExtractOptions, extract_region, slugify
from hiveflow_spreadsheet_parser.models import DraftManifest, DraftTable
from hiveflow_spreadsheet_parser.profile import profile_extraction
from hiveflow_spreadsheet_parser.readers.base import SheetGrid, Workbook


def build_draft_table(
    grid: SheetGrid,
    a1_range: str,
    *,
    kind: str = "table",
    confidence: float = 0.5,
    header_rows: int = 1,
    orientation: str = "rows",
    row_group_key: str | None = None,
    name: str | None = None,
    signals: list[str] | None = None,
) -> DraftTable:
    ex = extract_region(
        grid,
        a1_range,
        ExtractOptions(
            header_rows=header_rows, orientation=orientation, row_group_key=row_group_key
        ),
    )
    table_profile, columns = profile_extraction(ex)
    start = a1_range.split(":", 1)[0].lower()
    return DraftTable(
        id=f"{grid.name}!{a1_range}",
        name=name or f"{slugify(grid.name)}_{start}",
        sheet=grid.name,
        a1_range=a1_range,
        kind=kind,  # type: ignore[arg-type]
        confidence=round(confidence, 2),
        header_rows=ex.header_rows,
        orientation=orientation,  # type: ignore[arg-type]
        row_group_key=row_group_key,
        detector_signals=signals or [],
        profile=table_profile,
        columns=columns,
    )


def _from_region(grid: SheetGrid, region: Region) -> DraftTable:
    return build_draft_table(
        grid,
        region.a1_range,
        kind=region.kind,
        confidence=region.confidence,
        header_rows=region.header_rows,
        orientation=region.orientation,
        signals=region.signals,
    )


def build_draft(workbook: Workbook, source: SourceInfo) -> DraftManifest:
    tables: list[DraftTable] = []
    warnings: list[str] = []
    for grid in workbook.sheets:
        regions = detect_sheet(grid)
        if not regions:
            warnings.append(f"sheet {grid.name!r}: no structured regions detected")
        for region in regions:
            try:
                tables.append(_from_region(grid, region))
            except Exception as exc:
                warnings.append(f"sheet {grid.name!r} {region.a1_range}: {exc}")
    return DraftManifest(source=source, tables=tables, warnings=warnings)


def draft_for_file(path: str) -> DraftManifest:
    from hiveflow_spreadsheet_parser.readers import read_workbook

    wb = read_workbook(path)
    source = SourceInfo.for_file(path, fmt=wb.fmt, parser_version=__version__)
    return build_draft(wb, source)
