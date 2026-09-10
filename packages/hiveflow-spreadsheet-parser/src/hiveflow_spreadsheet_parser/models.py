"""Pydantic contracts: the draft manifest / review file and the final output."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hiveflow_core import SourceInfo

DType = Literal["int", "float", "decimal", "bool", "date", "datetime", "string"]
Semantic = Literal[
    "id",
    "email",
    "currency",
    "percent",
    "date",
    "datetime",
    "boolean",
    "category",
    "number",
    "free_text",
    "unknown",
]
Orientation = Literal["rows", "columns"]
RegionKind = Literal["table", "matrix", "key_value", "list", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ColumnProfile(_Model):
    source_name: str  # header text as found in the sheet
    name: str  # editable — final column name
    ordinal: int  # editable — output order
    dtype: DType  # editable
    semantic: Semantic = "unknown"  # editable
    include: bool = True  # editable — set false to drop

    nullable: bool = False
    null_count: int = 0
    populated_pct: float = 100.0
    distinct_count: int = 0
    is_unique: bool = False
    detected_format: str | None = None
    sample_values: list[Any] = Field(default_factory=list)
    top_values: list[tuple[Any, int]] = Field(default_factory=list)
    min: Any = None
    max: Any = None
    mean: float | None = None
    char_min: int | None = None
    char_max: int | None = None


class TableProfile(_Model):
    row_count: int
    column_count: int
    empty_row_count: int = 0
    duplicate_row_count: int = 0
    candidate_key: list[str] = Field(default_factory=list)
    header_rows: int = 1
    has_totals_row: bool = False


class DraftTable(_Model):
    id: str  # stable: "<sheet>!<a1_range>"
    name: str  # editable — becomes the CSV filename
    sheet: str
    a1_range: str  # editable — re-extracted on apply if changed
    kind: RegionKind
    confidence: float
    header_rows: int  # editable
    orientation: Orientation = "rows"  # editable
    row_group_key: str | None = None  # editable — source column that starts a new record

    include: bool = True  # editable
    approved: bool = False  # must be true for `apply` to emit a CSV
    dedupe_on: list[str] = Field(default_factory=list)  # editable — drop dup rows on these cols
    drop_duplicate_rows: bool = False  # editable — drop fully-identical rows
    agent_notes: str = ""
    cleanup_instructions: str = ""  # free-text -> agent pass on `apply`

    detector_signals: list[str] = Field(default_factory=list)
    profile: TableProfile
    columns: list[ColumnProfile]


class SkippedRegion(_Model):
    sheet: str
    a1_range: str
    reason: str


class DraftManifest(_Model):
    """What `propose` writes and a human edits: ``<file>.review.yaml``."""

    kind: Literal["hiveflow.spreadsheet-parser/review"] = "hiveflow.spreadsheet-parser/review"
    version: int = 1
    source: SourceInfo
    tables: list[DraftTable] = Field(default_factory=list)
    skipped: list[SkippedRegion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --- final output of `apply` ---------------------------------------------------


class AppliedColumn(_Model):
    name: str
    dtype: DType
    semantic: Semantic = "unknown"


class AppliedTable(_Model):
    name: str
    sheet: str
    a1_range: str
    csv_path: str
    schema_: list[AppliedColumn] = Field(alias="schema")
    row_count_in: int
    row_count_out: int
    profile_before: TableProfile
    profile_after: TableProfile
    transforms_applied: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    agent_cost_usd: float | None = None  # refine-agent Bedrock spend for this table


class OutputManifest(_Model):
    kind: Literal["hiveflow.spreadsheet-parser/output"] = "hiveflow.spreadsheet-parser/output"
    version: int = 1
    source: SourceInfo
    tables: list[AppliedTable] = Field(default_factory=list)
    skipped: list[SkippedRegion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
