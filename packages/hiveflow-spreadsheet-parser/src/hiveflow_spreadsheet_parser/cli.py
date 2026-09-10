"""hiveflow-parse: propose tables, review, then apply to cleaned CSVs."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

from hiveflow_spreadsheet_parser.agent import propose
from hiveflow_spreadsheet_parser.models import DraftTable
from hiveflow_spreadsheet_parser.readers import read_workbook
from hiveflow_spreadsheet_parser.refine_agent import refine_table
from hiveflow_spreadsheet_parser.review import (
    RefineFn,
    ReviewError,
    apply_review,
    dump_review,
    load_review,
    validate_review,
)

REVIEW_SUFFIX = ".review.yaml"


def _review_path(file: Path, out: str | None) -> Path:
    return Path(out) if out else file.with_name(file.name + REVIEW_SUFFIX)


def _source_file(review_path: Path, override: str | None) -> Path:
    if override:
        return Path(override)
    name = review_path.name
    if name.endswith(REVIEW_SUFFIX):
        return review_path.with_name(name[: -len(REVIEW_SUFFIX)])
    raise SystemExit(f"cannot infer the source workbook from {review_path.name!r}; pass --file")


def cmd_propose(args: argparse.Namespace) -> int:
    file = Path(args.file)
    if not file.is_file():
        raise SystemExit(f"no such file: {file}")
    result = asyncio.run(
        propose(
            str(file),
            model=args.model,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            use_agent=not args.no_agent,
        )
    )
    dest = _review_path(file, args.out)
    dump_review(result.manifest, dest)
    m = result.manifest
    print(
        f"{'agent' if result.used_agent else 'heuristic'}: "
        f"{len(m.tables)} table(s), {len(m.skipped)} skipped -> {dest}"
    )
    for t in m.tables:
        print(f"  {t.sheet}!{t.a1_range:12} {t.kind:9} {t.profile.row_count:>5} rows  {t.name}")
    for w in m.warnings:
        print(f"  ! {w}")
    if result.run is not None and result.run.total_cost_usd is not None:
        print(f"  agent cost: ${result.run.total_cost_usd:.4f}")
    print(
        f"\nEdit {dest.name}, set `approved: true`, then:\n"
        f"  hiveflow-parse apply {dest.name} --out out/"
    )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    review_path = Path(args.review_file)
    if not review_path.is_file():
        raise SystemExit(f"no such file: {review_path}")
    try:
        manifest = load_review(review_path)
    except ReviewError as exc:
        raise SystemExit(str(exc)) from exc

    errors = validate_review(manifest)
    if errors and not args.force:
        print("review file has problems:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    source = _source_file(review_path, args.file)
    if not source.is_file():
        raise SystemExit(f"source workbook not found: {source} (pass --file)")
    workbook = read_workbook(str(source))

    refine: RefineFn | None = None
    if not args.no_refine:
        model = args.model
        max_budget_usd = args.max_budget_usd

        def refine(
            table: DraftTable, frame: pd.DataFrame
        ) -> tuple[pd.DataFrame, list[str], float | None]:
            return asyncio.run(
                refine_table(table, frame, model=model, max_budget_usd=max_budget_usd)
            )

    output = apply_review(manifest, workbook, args.out, refine=refine)
    print(f"wrote {len(output.tables)} CSV(s) to {args.out}/")
    for at in output.tables:
        notes = list(at.warnings)
        if at.agent_cost_usd is not None:
            notes.append(f"agent cost: ${at.agent_cost_usd:.4f}")
        note = f"  ({', '.join(notes)})" if notes else ""
        print(f"  {at.csv_path:28} {at.row_count_out:>5} rows / {len(at.schema_)} cols{note}")
    for s in output.skipped:
        print(f"  - skipped {s.sheet}!{s.a1_range}: {s.reason}")
    print(f"manifest: {args.out}/{Path(manifest.source.filename).stem}.manifest.json")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    file = Path(args.file)
    propose_args = argparse.Namespace(
        file=str(file),
        out=None,
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        no_agent=args.no_agent,
    )
    rc = cmd_propose(propose_args)
    if rc:
        return rc
    review_path = _review_path(file, None)
    editor = os.environ.get("EDITOR")
    if editor and sys.stdin.isatty():
        input(f"\nOpening {review_path.name} in $EDITOR. Save + close to continue... ")
        subprocess.run([editor, str(review_path)], check=False)
    else:
        input(f"\nEdit {review_path}, set `approved: true`, then press Enter to apply... ")
    apply_args = argparse.Namespace(
        review_file=str(review_path),
        file=str(file),
        out=args.out,
        model=args.model,
        max_budget_usd=args.max_budget_usd,
        no_refine=args.no_refine,
        force=args.force,
    )
    return cmd_apply(apply_args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hiveflow-parse", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("propose", help="discover + profile tables -> <file>.review.yaml")
    pr.add_argument("file")
    pr.add_argument("--out", help="review file path (default: <file>.review.yaml)")
    pr.add_argument("--model", help="Bedrock model id / alias override")
    pr.add_argument("--max-turns", type=int, default=80)
    pr.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="hard USD spend ceiling for this run (default: MAX_BUDGET_USD from .env, else none)",
    )
    pr.add_argument("--no-agent", action="store_true", help="heuristic detection only")
    pr.set_defaults(func=cmd_propose)

    pa = sub.add_parser("apply", help="review.yaml -> cleaned CSV per approved table")
    pa.add_argument("review_file")
    pa.add_argument("--out", default="out", help="output directory (default: out)")
    pa.add_argument("--file", help="source workbook (default: review path minus .review.yaml)")
    pa.add_argument("--model", help="Bedrock model id / alias override")
    pa.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="hard USD spend ceiling for the refine pass (default: MAX_BUDGET_USD from .env)",
    )
    pa.add_argument("--no-refine", action="store_true", help="skip cleanup_instructions agent pass")
    pa.add_argument("--force", action="store_true", help="apply despite validation problems")
    pa.set_defaults(func=cmd_apply)

    rn = sub.add_parser("run", help="propose, wait for edits, then apply")
    rn.add_argument("file")
    rn.add_argument("--out", default="out")
    rn.add_argument("--model")
    rn.add_argument("--max-turns", type=int, default=80)
    rn.add_argument("--max-budget-usd", type=float, default=None, help="hard USD spend ceiling")
    rn.add_argument("--no-agent", action="store_true")
    rn.add_argument("--no-refine", action="store_true")
    rn.add_argument("--force", action="store_true")
    rn.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
