"""Read/write ``<file>.review.yaml`` and run the `apply` phase from it."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import yaml
from pydantic import ValidationError

from hiveflow_core import json_default
from hiveflow_spreadsheet_parser.clean import clean_table, frame_to_csv_value
from hiveflow_spreadsheet_parser.extract import slugify
from hiveflow_spreadsheet_parser.models import (
    AppliedColumn,
    AppliedTable,
    DraftManifest,
    DraftTable,
    DType,
    OutputManifest,
    Semantic,
    SkippedRegion,
)
from hiveflow_spreadsheet_parser.readers.base import Workbook, parse_a1_range

RefineFn = Callable[[DraftTable, pd.DataFrame], tuple[pd.DataFrame, list[str], float | None]]


class ReviewError(Exception):
    """A review file is malformed or internally inconsistent."""


def dump_review(manifest: DraftManifest, path: str | Path) -> Path:
    p = Path(path)
    data = manifest.model_dump(mode="json", by_alias=True)
    header = (
        "# HiveFlow spreadsheet-parser — review file.\n"
        "# Edit column names / dtype / semantic / include / ordinal, adjust a1_range,\n"
        "# header_rows, or row_group_key (for records wrapped across multiple rows),\n"
        "# then set `approved: true` on the tables you want written.\n"
        "# Run:  hiveflow-parse apply <this-file> --out out/\n"
    )
    p.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return p


def load_review(path: str | Path) -> DraftManifest:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ReviewError(f"{path}: expected a YAML mapping at the top level")
    try:
        return DraftManifest.model_validate(raw)
    except ValidationError as exc:
        raise ReviewError(f"{path}: {exc.error_count()} problem(s)\n{exc}") from exc


def validate_review(manifest: DraftManifest) -> list[str]:
    """Semantic checks past schema validation. Empty list == good to apply."""
    errors: list[str] = []
    approved_names: dict[str, int] = {}
    for t in manifest.tables:
        if not t.approved:
            continue
        try:
            parse_a1_range(t.a1_range)
        except ValueError as exc:
            errors.append(f"{t.id}: bad a1_range {t.a1_range!r} ({exc})")
        included = [c for c in t.columns if c.include]
        if not included:
            errors.append(f"{t.id}: approved but every column is excluded")
        ordinals = [c.ordinal for c in included]
        if len(set(ordinals)) != len(ordinals):
            errors.append(f"{t.id}: duplicate ordinal(s) among included columns")
        names = [c.name for c in included]
        if len(set(names)) != len(names):
            errors.append(f"{t.id}: duplicate output column name(s): {names}")
        for key in t.dedupe_on:
            if key not in names:
                errors.append(f"{t.id}: dedupe_on {key!r} is not an included column")
        slug = slugify(t.name, fallback="table")
        approved_names.setdefault(slug, 0)
        approved_names[slug] += 1
    for slug, count in approved_names.items():
        if count > 1:
            errors.append(f"{count} approved tables share the CSV name {slug!r}")
    if not approved_names:
        errors.append("no tables are approved; nothing to write")
    return errors


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.map(frame_to_csv_value).to_csv(path, index=False, encoding="utf-8")


def apply_review(
    manifest: DraftManifest,
    workbook: Workbook,
    out_dir: str | Path,
    *,
    refine: RefineFn | None = None,
) -> OutputManifest:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    applied: list[AppliedTable] = []
    skipped: list[SkippedRegion] = list(manifest.skipped)
    warnings: list[str] = list(manifest.warnings)
    used_names: set[str] = set()

    for t in manifest.tables:
        if not (t.approved and t.include):
            skipped.append(
                SkippedRegion(
                    sheet=t.sheet,
                    a1_range=t.a1_range,
                    reason="not approved" if not t.approved else "include=false",
                )
            )
            continue
        try:
            grid = workbook.sheet(t.sheet)
        except KeyError:
            warnings.append(f"{t.id}: sheet not found in workbook; skipped")
            continue

        result = clean_table(grid, t)
        transforms = list(result.transforms)
        table_warnings = list(result.warnings)
        frame = result.frame
        agent_cost_usd: float | None = None

        if t.cleanup_instructions.strip() and refine is not None:
            frame, extra, agent_cost_usd = refine(t, frame)
            transforms.extend(extra)

        slug = slugify(t.name, fallback="table")
        if slug in used_names:
            slug = f"{slug}_{len(used_names)}"
        used_names.add(slug)
        csv_path = out / f"{slug}.csv"
        _write_csv(frame, csv_path)

        applied.append(
            AppliedTable(
                name=t.name,
                sheet=t.sheet,
                a1_range=t.a1_range,
                csv_path=str(csv_path.relative_to(out))
                if csv_path.is_relative_to(out)
                else str(csv_path),
                schema=[
                    AppliedColumn(
                        name=str(c), dtype=_dtype_for(t, str(c)), semantic=_sem_for(t, str(c))
                    )
                    for c in frame.columns
                ],
                row_count_in=result.row_count_in,
                row_count_out=len(frame),
                profile_before=result.profile_before,
                profile_after=result.profile_after,
                transforms_applied=transforms,
                warnings=table_warnings,
                agent_cost_usd=agent_cost_usd,
            )
        )

    output = OutputManifest(
        source=manifest.source, tables=applied, skipped=skipped, warnings=warnings
    )
    stem = Path(manifest.source.filename).stem
    (out / f"{stem}.manifest.json").write_text(
        json.dumps(output.model_dump(mode="json", by_alias=True), indent=2, default=json_default),
        encoding="utf-8",
    )
    return output


def _dtype_for(table: DraftTable, name: str) -> DType:
    for c in table.columns:
        if c.name == name:
            return c.dtype
    return "string"


def _sem_for(table: DraftTable, name: str) -> Semantic:
    for c in table.columns:
        if c.name == name:
            return c.semantic
    return "unknown"
