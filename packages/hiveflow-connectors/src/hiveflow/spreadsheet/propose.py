"""Bedrock + heuristic transformation proposals for Spreadsheet Engine."""

from __future__ import annotations

from typing import Any, Callable

from hiveflow.spreadsheet.synthesize import propose_clean_goal
from hiveflow.spreadsheet.stages import table_pipeline_stage
from hiveflow.spreadsheet.transform import (
    build_output_shape,
    compute_input_shape,
    empty_transformation,
    shape_compatibility,
    slugify_filename,
)


def _catalog_reuse_transformation(
    *,
    table: dict[str, Any],
    parse_table: dict[str, Any],
    linked_catalog: dict[str, Any] | None = None,
    knowledge_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Reuse a prior approved transformation when shape matches closely."""
    input_shape = compute_input_shape(parse_table)
    ref_spec = None
    ref_input = None
    reused_from = ""
    if linked_catalog:
        ref_spec = linked_catalog.get("transformation") or {}
        ref_input = ref_spec.get("input_shape") or linked_catalog.get("input_shape")
        reused_from = str(linked_catalog.get("catalog_id") or "")
    elif knowledge_entries:
        entry = knowledge_entries[0]
        ref_spec = entry.get("transformation") or {}
        ref_input = ref_spec.get("input_shape") or entry.get("input_shape")
        reused_from = str(entry.get("catalog_id") or entry.get("knowledge_id") or "")

    if not ref_spec or not ref_input:
        return None
    score, drift = shape_compatibility(input_shape, ref_input)
    ref_steps = list(ref_spec.get("steps") or [])
    if score < 0.8 or not ref_steps:
        return None

    output_shape = ref_spec.get("output_shape") or build_output_shape(table)
    return {
        "clean_goal": {
            "headers": [
                str(col.get("name") or "")
                for col in (output_shape.get("schema") or [])
                if isinstance(col, dict) and str(col.get("name") or "").strip()
            ],
            "rows": [],
            "row_count": 0,
            "preview_row_count": 0,
            "truncated": False,
            "grain": str(output_shape.get("grain") or table.get("grain") or ""),
            "notes": ["Reused from prior approved catalog entry"],
            "source": "catalog",
        },
        "clean_shape_status": "approved",
        "clean_shape_notes": [f"Shape reused from {reused_from}"],
        "transformation": {
            "version": 1,
            "steps": ref_steps,
            "input_shape": input_shape,
            "output_shape": output_shape,
        },
        "transformation_status": "pending_review",
        "transformation_confidence": round(min(0.95, 0.5 + score * 0.45), 2),
        "transformation_notes": [f"Reused {len(ref_steps)} transformation step(s) from prior approval"],
        "transformation_drift": drift,
        "reused_from_catalog_id": reused_from,
    }


def propose_transform_for_table(
    *,
    table: dict[str, Any],
    parse_table: dict[str, Any],
    linked_catalog: dict[str, Any] | None = None,
    knowledge_entries: list[dict[str, Any]] | None = None,
    sample: dict[str, Any] | None = None,
    invoke: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Propose a cleaned transformation for exactly one table.

    Split out of ``propose_transforms_for_report`` so a Step Functions Map state
    can fan a job's tables out across parallel Lambda invocations (each table's
    AI call can take minutes; a job with many tables would otherwise exceed the
    900s Lambda ceiling running them one after another in a single invocation).
    """
    kb = list(knowledge_entries or [])
    sample = dict(sample or {})
    if not sample.get("rows") and parse_table.get("sample_rows"):
        sample = {
            "headers": list(parse_table.get("headers") or []),
            "rows": list(parse_table.get("sample_rows") or []),
        }

    reused = _catalog_reuse_transformation(
        table=table,
        parse_table=parse_table,
        linked_catalog=linked_catalog,
        knowledge_entries=kb,
    )
    if reused:
        proposal = reused
    elif sample.get("rows"):
        headers = [
            str(name)
            for name in (sample.get("headers") or parse_table.get("headers") or [])
        ]
        proposal = propose_clean_goal(
            headers=headers,
            rows=list(sample.get("rows") or []),
            table=table,
            invoke=invoke,
        )
    else:
        proposal = {
            "clean_goal": {
                "headers": list(parse_table.get("headers") or []),
                "rows": [],
                "row_count": 0,
                "preview_row_count": 0,
                "truncated": False,
                "grain": str(table.get("grain") or ""),
                "notes": ["No sample rows available for cleaning"],
                "source": "empty",
            },
            "clean_shape_status": "pending_review",
            "clean_shape_notes": ["No sample rows available"],
            "transformation": empty_transformation(),
            "transformation_status": "awaiting_shape",
            "transformation_confidence": 0.0,
            "transformation_notes": [],
            "transformation_drift": [],
        }

    input_shape = compute_input_shape(parse_table) if parse_table else {}
    transformation = proposal.get("transformation") or empty_transformation()
    if input_shape and not transformation.get("input_shape"):
        transformation["input_shape"] = input_shape
    if not transformation.get("output_shape"):
        goal = proposal.get("clean_goal") or {}
        goal_headers = [str(h) for h in (goal.get("headers") or []) if str(h).strip()]
        if goal_headers:
            transformation["output_shape"] = {
                "entity_name": str(table.get("entity_name") or ""),
                "grain": str(goal.get("grain") or table.get("grain") or ""),
                "schema": [{"name": name, "type": "string"} for name in goal_headers],
            }
        else:
            transformation["output_shape"] = build_output_shape(table)
    proposal["transformation"] = transformation

    grain = str((proposal.get("clean_goal") or {}).get("grain") or "").strip()
    updated = {**table, **proposal}
    if grain:
        updated["grain"] = grain
    updated["pipeline_stage"] = table_pipeline_stage(updated)
    return updated


def propose_transforms_for_report(
    report: dict[str, Any],
    parse_payload: dict[str, Any],
    *,
    linked_catalog: dict[str, Any] | None = None,
    knowledge_entries: list[dict[str, Any]] | None = None,
    profile_payload: dict[str, Any] | None = None,
    table_samples: dict[str, dict[str, Any]] | None = None,
    invoke: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Propose cleaned shapes for each table (transform steps come after shape approve).

    Sequential — used by the local/dev synchronous pipeline only. The deployed
    Step Functions pipeline fans tables out via ``propose_transform_for_table``
    instead (see ``run_propose_table`` / ``run_propose_finalize`` in ``jobs.py``).
    """
    del profile_payload  # reserved for future clean-path hints
    parse_tables = {
        str(t.get("table_id")): t
        for t in (parse_payload.get("tables") or [])
        if isinstance(t, dict) and t.get("table_id")
    }
    kb = list(knowledge_entries or [])
    samples = dict(table_samples or {})

    updated_tables: list[dict[str, Any]] = []
    for table in report.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "")
        parse_table = parse_tables.get(table_id) or {}
        updated = propose_transform_for_table(
            table=table,
            parse_table=parse_table,
            linked_catalog=linked_catalog,
            knowledge_entries=kb,
            sample=samples.get(table_id) or {},
            invoke=invoke,
        )
        updated_tables.append(updated)

    report["tables"] = updated_tables
    report["table_count"] = len(updated_tables)
    return report


def propose_transforms(
    parse_payload: dict[str, Any],
    profile_payload: dict[str, Any],
    report: dict[str, Any],
    *,
    linked_catalog: dict[str, Any] | None = None,
    knowledge_entries: list[dict[str, Any]] | None = None,
    table_samples: dict[str, dict[str, Any]] | None = None,
    invoke: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Propose cleaned shapes for an interpreted report."""
    filename = str(parse_payload.get("filename") or report.get("filename") or "")
    report = propose_transforms_for_report(
        report,
        parse_payload,
        linked_catalog=linked_catalog,
        knowledge_entries=knowledge_entries,
        profile_payload=profile_payload,
        table_samples=table_samples,
        invoke=invoke,
    )
    report["filename"] = filename
    report["source_file_slug"] = slugify_filename(filename)
    return report
