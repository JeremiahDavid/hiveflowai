"""Spreadsheet Engine Source Browser UI — aligned with DNA Engine layout."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Callable

from markupsafe import Markup

from hiveflow.dna.source_docs.reference import normalize_reference_source
from hiveflow.dna.web.portal.dna_nav import source_docs_inspector_path
from hiveflow.dna.web.portal.semantics.source_docs_render import _source_switcher
from hiveflow.dna.web.portal.spreadsheet_engine.service import spreadsheet_pipeline_progress
from hiveflow.dna.web.templating import render_template

_IN_FLIGHT_JOB_STATUSES = frozenset(
    {
        "uploaded",
        "running",
        "parsing",
        "parsed",
        "profiling",
        "profiled",
        "interpreting",
        "interpreted",
        "proposing",
    }
)


def _active_proposal_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in tables
        if isinstance(item, dict) and str(item.get("status") or "") != "discarded"
    ]


def _json_for_script(payload: Any) -> str:
    return json.dumps(payload).replace("<", "\\u003c")


def _proposal_url(
    url: Callable[[str], str],
    *,
    source: str,
    job_id: str,
    table_index: int = 0,
) -> str:
    return url(
        f"{source_docs_inspector_path(source)}"
        f"?job_id={job_id}&tab=review&table_index={table_index}"
    )


def _job_status_short(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "").strip().lower()
    if status in _IN_FLIGHT_JOB_STATUSES:
        return "Generating"
    if status == "awaiting_sheets":
        return "Select sheets"
    if status == "error":
        return "Error"
    if status == "ready":
        return "Ready"
    return status.replace("_", " ").title() or "Uploaded"


def _catalog_url(
    url: Callable[[str], str],
    *,
    source: str,
    catalog_id: str = "",
) -> str:
    path = f"{source_docs_inspector_path(source)}?tab=catalog"
    if catalog_id:
        path += f"&catalog_id={catalog_id}"
    return url(path)


def _chat_html(
    table: dict[str, Any] | None,
    *,
    analyzing: bool = False,
    entity_name: str = "",
) -> str:
    history = list((table or {}).get("chat_history") or [])
    html = ""
    for entry in history:
        role = str(entry.get("role") or "user").strip().lower()
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        label = "You" if role == "user" else "Assistant"
        css = "assistant-bubble user" if role == "user" else "assistant-bubble"
        html += (
            f'<div class="{css}">'
            f'<div class="assistant-bubble-label">{escape(label)}</div>'
            f'<div class="assistant-bubble-text">{escape(text)}</div>'
            "</div>"
        )
    if analyzing:
        html += (
            '<div class="assistant-bubble" id="spreadsheet-job-running">'
            '<div class="assistant-bubble-label">Assistant</div>'
            '<div class="assistant-bubble-text">Analyzing workbook — parsing sheets, profiling columns, and drafting schema proposals…</div>'
            "</div>"
        )
    if html:
        return html
    label = entity_name or "this table"
    return (
        '<p class="pack-card-lead">'
        f"Ask the assistant to refine grain, column names, types, or relationships for {escape(label)}."
        "</p>"
    )


def _schema_table_html(schema: list[dict[str, Any]]) -> str:
    rows = []
    for col in schema:
        if not isinstance(col, dict):
            continue
        flags = []
        if col.get("is_key"):
            flags.append("key")
        if col.get("is_foreign_key"):
            flags.append("fk")
        if col.get("nullable"):
            flags.append("nullable")
        rows.append(
            {
                "name": str(col.get("name") or ""),
                "type": str(col.get("type") or ""),
                "description": str(col.get("description") or ""),
                "flags": ", ".join(flags) if flags else "—",
            }
        )
    if not rows:
        return '<p class="muted">No schema columns proposed.</p>'
    return render_template("portal/spreadsheet_engine/_schema_table.html", rows=rows)


def _profiling_table_html(profiling: dict[str, Any]) -> str:
    columns = profiling.get("columns") or []
    if not columns:
        return ""
    rows = []
    for col in columns:
        if not isinstance(col, dict):
            continue
        rows.append(
            {
                "name": str(col.get("name") or ""),
                "type": str(col.get("inferred_type") or ""),
                "null_rate": f"{float(col.get('null_rate') or 0):.0%}",
                "cardinality": int(col.get("cardinality") or 0),
                "is_key": bool(col.get("likely_key")),
                "patterns": ", ".join(col.get("patterns") or []) or "—",
            }
        )
    return render_template("portal/spreadsheet_engine/_profiling_table.html", rows=rows)


def _schema_profiling_panel_html(schema: list[dict[str, Any]], profiling: dict[str, Any]) -> str:
    schema_table = Markup(_schema_table_html(schema))
    profiling_table_raw = _profiling_table_html(profiling)
    if not profiling_table_raw:
        return render_template(
            "portal/spreadsheet_engine/_schema_panel_simple.html", schema_table=schema_table
        )
    return render_template(
        "portal/spreadsheet_engine/_schema_panel_tabs.html",
        schema_table=schema_table,
        profiling_table=Markup(profiling_table_raw),
    )


def _join_proposals_panel_html(
    table: dict[str, Any],
    *,
    job_id: str = "",
    table_index: int = 0,
    readonly: bool = False,
) -> str:
    from hiveflow.spreadsheet.stages import table_pipeline_stage

    stage = table_pipeline_stage(table)
    proposals = [item for item in (table.get("join_proposals") or []) if isinstance(item, dict)]
    if stage not in {"join_review", "joins_approved", "catalogued"} and not proposals:
        return ""
    status = str(table.get("join_status") or "pending_review")
    notes_html = _bullet_notes_html(list(table.get("join_notes") or []))
    editable = not readonly and bool(job_id) and status != "approved"
    rows = ""
    for item in proposals:
        pid = str(item.get("id") or "")
        layer = str(item.get("layer") or "")
        target = str(item.get("target") or "")
        left_key = str(item.get("left_key") or "")
        right_key = str(item.get("right_key") or "")
        reason = str(item.get("match_reason") or "")
        confidence = float(item.get("confidence") or 0)
        selected = bool(item.get("selected", True))
        check = " checked" if selected else ""
        if editable:
            use_cell = f'<td><input type="checkbox" name="join_id" value="{escape(pid)}"{check} /></td>'
        else:
            use_cell = f"<td>{'yes' if selected else ''}</td>"
        rows += (
            "<tr>"
            f"{use_cell}"
            f"<td><span class=\"kpi-chip\">{escape(layer)}</span></td>"
            f"<td><code>{escape(target)}</code></td>"
            f"<td><code>{escape(left_key)}</code> → <code>{escape(right_key)}</code></td>"
            f"<td>{confidence:.0%}</td>"
            f"<td>{escape(reason)}</td>"
            "</tr>"
        )
    if rows:
        table_html = (
            '<div class="semantic-builder-scroll table-wrap">'
            '<table class="semantic-builder-table">'
            "<thead><tr><th>Use</th><th>Layer</th><th>Target</th><th>Keys</th><th>Conf.</th><th>Why</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        table_html = (
            '<p class="muted">DNA Engine did not find silver or gold join targets for this grain and key.</p>'
        )

    table_id = str(table.get("table_id") or "")
    body = table_html
    if editable:
        body = (
            '<form method="post" class="spreadsheet-join-form">'
            '<input type="hidden" name="action" value="approve_joins" />'
            f'<input type="hidden" name="job_id" value="{escape(job_id)}" />'
            f'<input type="hidden" name="table_id" value="{escape(table_id)}" />'
            f'<input type="hidden" name="table_index" value="{table_index}" />'
            f"{table_html}"
            '<div class="spreadsheet-transform-actions">'
            '<button type="submit" class="btn btn-primary">Approve selected joins</button>'
            "</div></form>"
            + _approve_reject_actions_html(
                job_id=job_id,
                table_id=table_id,
                table_index=table_index,
                approve_action="refresh_joins",
                reject_action="reject_joins",
                approve_label="Re-run DNA joins",
                reject_placeholder="What join is missing or wrong?",
            )
            + '<p class="muted">DNA Engine matched this table\'s grain and keys to silver and existing gold tables.</p>'
        )

    title = "Step 5 — Lake joins"
    if status == "approved":
        title = "Step 5 — Lake joins (approved)"
    status_chip = (
        '<div class="spreadsheet-transform-head-meta">'
        f'<span class="kpi-chip">{escape(status.replace("_", " "))}</span>'
        "</div>"
    )
    return f"""
    <section class="spreadsheet-transform-panel spreadsheet-join-panel" id="spreadsheet-join-proposals">
      <div class="spreadsheet-transform-head">
        <h3 class="kpi-section-heading">{escape(title)}</h3>
        {status_chip}
      </div>
      {notes_html}
      {body}
    </section>
    """


def _relationships_html(relationships: list[dict[str, Any]]) -> str:
    items = []
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        entity = str(rel.get("to_entity") or "").strip()
        column = str(rel.get("via_column") or "").strip()
        if not entity or not column:
            continue
        items.append(
            {
                "entity": entity,
                "column": column,
                "confidence": f"{float(rel.get('confidence') or 0):.0%}",
            }
        )
    if not items:
        return ""
    return render_template("portal/spreadsheet_engine/_relationships.html", items=items)


def _bullet_notes_html(notes: list[Any]) -> str:
    items = [text for note in notes if (text := str(note or "").strip())]
    if not items:
        return ""
    return render_template("portal/spreadsheet_engine/_bullet_notes.html", items=items)


def _notes_html(notes: list[Any]) -> str:
    items: list[str] = []
    for note in notes:
        text = str(note or "").strip()
        if not text:
            continue
        if "heuristic fallback" in text.lower():
            items.append("Schema inferred locally because AI interpretation was unavailable.")
            continue
        items.append(text)
    if not items:
        return ""
    return render_template(
        "portal/spreadsheet_engine/_notes.html", bullets=Markup(_bullet_notes_html(items))
    )


def _approve_reject_actions_html(
    *,
    job_id: str,
    table_id: str,
    table_index: int,
    approve_action: str,
    reject_action: str,
    approve_label: str,
    reject_label: str = "Reject",
    reject_placeholder: str = "What should the assistant change?",
    reason_id: str = "",
    extra_class: str = "",
) -> str:
    suffix = reason_id if reason_id else reject_action
    wrap_class = "spreadsheet-transform-actions"
    if extra_class:
        wrap_class = f"{wrap_class} {extra_class}"
    return render_template(
        "portal/spreadsheet_engine/_approve_reject_actions.html",
        wrap_class=wrap_class,
        job_id=job_id,
        table_id=table_id,
        table_index=table_index,
        approve_action=approve_action or None,
        approve_label=approve_label,
        reject_action=reject_action,
        reject_label=reject_label,
        reject_placeholder=reject_placeholder,
        reason_field_id=f"spreadsheet-reject-reason-{suffix}",
    )


def _format_step_detail(step: dict[str, Any]) -> str:
    rest = {k: v for k, v in step.items() if k != "op"}
    mapping = rest.get("mapping")
    if isinstance(mapping, dict) and mapping:
        pairs = [f"{src} → {dst}" for src, dst in mapping.items()]
        text = ", ".join(pairs)
        return text if len(text) <= 220 else text[:217] + "…"
    dumped = json.dumps(rest, default=str, separators=(", ", ": "))
    return dumped if len(dumped) <= 220 else dumped[:217] + "…"


def _transformation_steps_html(transformation: dict[str, Any]) -> str:
    steps = transformation.get("steps") or []
    if not steps:
        return '<p class="muted">No transformation steps.</p>'
    items = [
        {"op": str(step.get("op") or ""), "detail": _format_step_detail(step)}
        for step in steps
        if isinstance(step, dict)
    ]
    return render_template("portal/spreadsheet_engine/_transformation_steps.html", items=items)


def _transform_preview_diff_html(transform_preview: dict[str, Any] | None) -> str:
    if not transform_preview:
        return ""
    before = transform_preview.get("before") or {}
    after = transform_preview.get("after") or {}
    if not before.get("rows") and not after.get("rows"):
        return ""
    before_html = _preview_html(before, heading="")
    after_html = _preview_html(after, heading="")
    if not before_html and not after_html:
        return ""
    return render_template(
        "portal/spreadsheet_engine/_transform_preview_diff.html",
        before_html=Markup(before_html) if before_html else None,
        after_html=Markup(after_html) if after_html else None,
    )


def _pipeline_stage_stepper_html(table: dict[str, Any]) -> str:
    from hiveflow.spreadsheet.stages import PIPELINE_STAGES, stage_index, table_pipeline_stage

    current = table_pipeline_stage(table)
    current_idx = stage_index(current)
    items = []
    for idx, (key, label) in enumerate(PIPELINE_STAGES):
        if idx < current_idx:
            state = "is-done"
        elif idx == current_idx:
            state = "is-active"
        else:
            state = "is-todo"
        items.append(
            f'<li class="spreadsheet-stage-step {state}" data-stage="{escape(key)}">'
            f"<span>{escape(label)}</span></li>"
        )
    return (
        '<ol class="spreadsheet-stage-stepper" aria-label="Table review stages">'
        + "".join(items)
        + "</ol>"
    )


def _transformation_panel_html(
    table: dict[str, Any],
    *,
    job_id: str = "",
    table_index: int = 0,
    url: Callable[[str], str] | None = None,
    source: str = "",
    readonly: bool = False,
    transform_preview: dict[str, Any] | None = None,
) -> str:
    del source  # reserved for deep-links
    transformation = table.get("transformation") or {}
    steps = transformation.get("steps") or []
    if not steps and readonly:
        return ""
    status = str(table.get("transformation_status") or "pending_review")
    drift = list(table.get("transformation_drift") or [])
    notes = list(table.get("transformation_notes") or [])
    confidence = float(table.get("transformation_confidence") or 0)
    drift_html = ""
    if drift:
        drift_html = '<ul class="spreadsheet-transform-drift">' + "".join(
            f"<li>{escape(item)}</li>" for item in drift
        ) + "</ul>"
    notes_html = _bullet_notes_html(notes)
    actions = ""
    if not readonly and url and job_id and steps and status != "approved":
        table_id = str(table.get("table_id") or "")
        if status == "rejected":
            hint = (
                '<p class="muted">Rejected — add details in the box next to Reject, or use chat at the bottom. '
                "The assistant has this proposal as context.</p>"
            )
        else:
            hint = (
                '<p class="muted">Compare the deterministic output to your approved AI cleaned goal. Approving saves this transformation for future uploads.</p>'
            )
        actions = (
            _approve_reject_actions_html(
                job_id=job_id,
                table_id=table_id,
                table_index=table_index,
                approve_action="approve_transformation",
                reject_action="reject_transformation",
                approve_label="Approve transform output",
                reject_placeholder="What should the transform change?",
            )
            + hint
            + f"""
        <details class="spreadsheet-transform-advanced">
          <summary>Edit transformation JSON</summary>
          <form method="post" class="spreadsheet-transform-edit-form">
            <input type="hidden" name="action" value="edit_transformation" />
            <input type="hidden" name="job_id" value="{escape(job_id)}" />
            <input type="hidden" name="table_id" value="{escape(table_id)}" />
            <input type="hidden" name="table_index" value="{table_index}" />
            <textarea id="spreadsheet-transform-json" name="transformation_json" rows="6" class="spreadsheet-transform-json">{escape(json.dumps(transformation, indent=2, default=str))}</textarea>
            <button type="submit" class="btn btn-secondary">Save edits for review</button>
          </form>
        </details>
        """
        )
    elif status == "approved":
        actions = '<p class="muted">Transformation approved and saved for reuse.</p>'
    status_chip = (
        '<div class="spreadsheet-transform-head-meta">'
        f'<span class="kpi-chip">{escape(status.replace("_", " "))}</span>'
    )
    if confidence:
        status_chip += f'<span class="muted">Confidence {confidence:.0%}</span>'
    status_chip += "</div>"

    # Prefer goal-vs-transform comparison when clean_goal exists.
    clean_goal = table.get("clean_goal") or {}
    preview_payload = (
        (transform_preview or {}).get("transformation_preview")
        if isinstance(transform_preview, dict) and transform_preview.get("transformation_preview")
        else transform_preview
    )
    if clean_goal.get("rows") and isinstance(preview_payload, dict):
        after = preview_payload.get("after") or {}
        goal_preview = {
            "headers": list(clean_goal.get("headers") or []),
            "rows": list(clean_goal.get("rows") or []),
            "row_count": int(clean_goal.get("row_count") or len(clean_goal.get("rows") or [])),
            "preview_row_count": int(
                clean_goal.get("preview_row_count") or len(clean_goal.get("rows") or [])
            ),
            "truncated": bool(clean_goal.get("truncated")),
        }
        preview_block = _transform_preview_diff_html({"before": goal_preview, "after": after})
    else:
        preview_block = _transform_preview_diff_html(preview_payload)

    return f"""
    <section class="spreadsheet-transform-panel">
      <div class="spreadsheet-transform-head">
        <h3 class="kpi-section-heading">Step 2 — Deterministic transform</h3>
        {status_chip}
      </div>
      {drift_html}
      {notes_html}
      {_transformation_steps_html(transformation)}
      {preview_block}
      {actions}
    </section>
    """


def _coerce_preview_row(row: Any, headers: list[str]) -> list[Any] | None:
    if isinstance(row, list):
        return row
    if isinstance(row, dict):
        return [row.get(name, "") for name in headers] if headers else list(row.values())
    return None


def _preview_html(
    preview: dict[str, Any] | None,
    *,
    heading: str = "Data preview",
    max_rows: int | None = None,
    compact: bool = False,
) -> str:
    if not preview:
        return ""
    headers = [str(name) for name in (preview.get("headers") or []) if str(name).strip()]
    raw_rows = preview.get("rows") or []
    rows: list[list[Any]] = []
    for row in raw_rows:
        coerced = _coerce_preview_row(row, headers)
        if coerced is None:
            continue
        rows.append(coerced)
        if max_rows is not None and len(rows) >= max_rows:
            break
    if not headers and not rows:
        return ""
    header_cells = "".join(f"<th>{escape(name)}</th>" for name in headers)
    body_rows = ""
    for row in rows:
        cells = ""
        width = len(headers) if headers else len(row)
        for idx in range(width):
            value = row[idx] if idx < len(row) else ""
            if value is None:
                text = ""
            else:
                text = str(value)
            title_attr = f' title="{escape(text)}"' if len(text) > 48 else ""
            display = text if len(text) <= 80 else text[:77] + "…"
            cells += f"<td{title_attr}>{escape(display)}</td>"
        body_rows += f"<tr>{cells}</tr>"
    if not body_rows:
        empty = '<p class="muted">No preview rows.</p>'
        heading_html = f'<h3 class="kpi-section-heading">{escape(heading)}</h3>' if heading else ""
        return f"{heading_html}{empty}"
    total_rows = int(preview.get("row_count") or len(raw_rows) or 0)
    shown = len(rows)
    note = f"Showing {shown} of {total_rows} data rows." if total_rows else f"Showing {shown} row(s)."
    if preview.get("truncated") or (max_rows is not None and total_rows > shown):
        note += " Preview is condensed."
    heading_html = f'<h3 class="kpi-section-heading">{escape(heading)}</h3>' if heading else ""
    note_html = f'<p class="muted spreadsheet-preview-note">{escape(note)}</p>'
    compact_class = " is-condensed" if compact else ""
    return f"""
    {heading_html}
    {note_html}
    <div class="semantic-builder-scroll table-wrap spreadsheet-preview-table{compact_class}">
      <table class="semantic-builder-table">
        <thead><tr>{header_cells}</tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>
    """


def _clean_shape_panel_html(
    table: dict[str, Any],
    *,
    job_id: str = "",
    table_index: int = 0,
    url: Callable[[str], str] | None = None,
    source: str = "",
    readonly: bool = False,
) -> str:
    from hiveflow.spreadsheet.stages import table_pipeline_stage

    clean_goal = table.get("clean_goal") or {}
    if not isinstance(clean_goal, dict):
        clean_goal = {}
    if readonly and not clean_goal:
        return ""
    stage = table_pipeline_stage(table)
    status = str(table.get("clean_shape_status") or "pending_review")
    notes = list(table.get("clean_shape_notes") or clean_goal.get("notes") or [])
    notes_html = _bullet_notes_html(notes)
    preview = {
        "headers": list(clean_goal.get("headers") or []),
        "rows": list(clean_goal.get("rows") or []),
        "row_count": int(clean_goal.get("row_count") or len(clean_goal.get("rows") or [])),
        "preview_row_count": int(
            clean_goal.get("preview_row_count") or len(clean_goal.get("rows") or [])
        ),
        "truncated": bool(clean_goal.get("truncated")),
    }
    grain = str(clean_goal.get("grain") or table.get("grain") or "")
    grain_html = f'<p class="muted">Grain: {escape(grain)}</p>' if grain else ""
    preview_html = _preview_html(preview, heading="") if preview.get("headers") or preview.get("rows") else (
        '<p class="muted">No cleaned preview yet. Wait for AI to finish generating the proposal, or reject and describe what to fix.</p>'
    )
    actions = ""
    has_rows = bool(preview.get("rows"))
    if not readonly and url and job_id and stage == "clean_review":
        table_id = str(table.get("table_id") or "")
        if status == "rejected":
            hint = (
                '<p class="muted">Rejected — add details in the box next to Reject, or use chat at the bottom. '
                "The assistant has this proposal as context.</p>"
            )
        else:
            hint = (
                '<p class="muted">Approve this cleaned table, or reject with details for the assistant to fix.</p>'
            )
        actions = (
            _approve_reject_actions_html(
                job_id=job_id,
                table_id=table_id,
                table_index=table_index,
                approve_action="approve_clean_shape",
                reject_action="reject_clean_shape",
                approve_label="Approve cleaned data",
                reject_placeholder="What should change in the cleaned data?",
            )
            + hint
        )
        if not has_rows:
            actions = actions.replace(
                'class="btn btn-primary">Approve cleaned data</button>',
                'class="btn btn-primary" disabled>Approve cleaned data</button>',
            )
    status_chip = (
        '<div class="spreadsheet-transform-head-meta">'
        f'<span class="kpi-chip">{escape(status.replace("_", " "))}</span>'
    )
    source = str(clean_goal.get("source") or "")
    if source:
        status_chip += f'<span class="muted">via {escape(source)}</span>'
    status_chip += "</div>"
    title = "Step 1 — Cleaned preview"
    if stage != "clean_review" and status == "approved":
        title = "Step 1 — Cleaned preview (approved)"

    return f"""
    <section class="spreadsheet-transform-panel spreadsheet-clean-shape-panel" id="spreadsheet-cleaned-preview">
      <div class="spreadsheet-transform-head">
        <h3 class="kpi-section-heading">{escape(title)}</h3>
        {status_chip}
      </div>
      {grain_html}
      {notes_html}
      {preview_html}
      {actions}
    </section>
    """


def _schema_toggle_script() -> str:
    return """
<script>
(function () {
  var root = document.getElementById("spreadsheet-schema-toggle");
  if (!root) return;
  var tabs = root.querySelectorAll("[data-spreadsheet-schema-tab]");
  var panels = root.querySelectorAll("[data-spreadsheet-schema-panel]");
  function activate(name) {
    tabs.forEach(function (tab) {
      var active = tab.getAttribute("data-spreadsheet-schema-tab") === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    panels.forEach(function (panel) {
      var active = panel.getAttribute("data-spreadsheet-schema-panel") === name;
      panel.hidden = !active;
    });
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      activate(tab.getAttribute("data-spreadsheet-schema-tab") || "schema");
    });
  });
})();
</script>
"""


def _stats_html(table: dict[str, Any]) -> str:
    source = table.get("source") or {}
    items = [
        ("Grain", str(table.get("grain") or "—")),
        ("Sheet", str(source.get("sheet") or "—")),
        ("Rows", str(int(source.get("row_count") or 0))),
        ("Confidence", f"{float(table.get('confidence') or 0):.0%}"),
    ]
    cards = "".join(
        f'<div class="source-docs-stat"><span class="source-docs-stat-label">{escape(label)}</span>'
        f'<strong class="source-docs-stat-value">{escape(value)}</strong></div>'
        for label, value in items
    )
    return f'<div class="source-docs-summary">{cards}</div>'


def _reload_validation_html(
    table: dict[str, Any],
    *,
    job_id: str = "",
    table_index: int = 0,
    url: Callable[[str], str] | None = None,
    source: str = "",
) -> str:
    if not table.get("reload_mode"):
        return ""
    status = str(table.get("reload_validation_status") or "")
    issues = list(table.get("reload_validation_issues") or [])
    linked = str(table.get("linked_catalog_id") or table.get("reused_from_catalog_id") or "")
    table_id = str(table.get("table_id") or "")

    if status == "passed":
        return f"""
        <section class="spreadsheet-reload-validation is-passed">
          <h3 class="kpi-section-heading">Reload validation passed</h3>
          <p class="muted">Transformed output matches the approved schema for <code>{escape(linked)}</code>. No AI analysis was run.</p>
          <form method="post" class="assistant-approve-form">
            <input type="hidden" name="action" value="complete_reload" />
            <input type="hidden" name="job_id" value="{escape(job_id)}" />
            <input type="hidden" name="table_id" value="{escape(table_id)}" />
            <input type="hidden" name="table_index" value="{table_index}" />
            <button type="submit" class="btn btn-primary">Complete reload</button>
          </form>
        </section>
        """

    issue_html = ""
    if issues:
        issue_html = "<ul class=\"spreadsheet-reload-validation-issues\">" + "".join(
            f"<li>{escape(item)}</li>" for item in issues
        ) + "</ul>"

    recovery = ""
    if url and job_id and source:
        analyze_href = escape(url(f"{source_docs_inspector_path(source)}?tab=analyze"))
        recovery = f"""
        <div class="spreadsheet-reload-recovery">
          <p class="pack-card-lead">Choose how to proceed:</p>
          <div class="spreadsheet-reload-recovery-actions">
            <a class="btn btn-secondary" href="{analyze_href}">Upload a different file</a>
            <form method="post" class="spreadsheet-reload-recovery-form">
              <input type="hidden" name="action" value="request_schema_rewrite" />
              <input type="hidden" name="job_id" value="{escape(job_id)}" />
              <input type="hidden" name="table_index" value="{table_index}" />
              <button type="submit" class="btn btn-secondary">Rewrite schema with AI</button>
            </form>
            <form method="post" class="spreadsheet-reload-recovery-form">
              <input type="hidden" name="action" value="request_transformation_rewrite" />
              <input type="hidden" name="job_id" value="{escape(job_id)}" />
              <input type="hidden" name="table_index" value="{table_index}" />
              <button type="submit" class="btn btn-secondary">Propose new transformation with AI</button>
            </form>
          </div>
        </div>
        """

    return f"""
    <section class="spreadsheet-reload-validation is-failed">
      <h3 class="kpi-section-heading">Reload validation failed</h3>
      <p class="muted">The new file does not match the approved schema for <code>{escape(linked)}</code>. No AI analysis was run.</p>
      {issue_html}
      {recovery}
    </section>
    """


def _table_analysis_html(
    table: dict[str, Any],
    *,
    job_id: str = "",
    table_index: int = 0,
    total: int = 1,
    url: Callable[[str], str] | None = None,
    source: str = "",
    readonly: bool = False,
    catalog_meta: dict[str, Any] | None = None,
    embedded: bool = False,
    table_preview: dict[str, Any] | None = None,
    transform_preview: dict[str, Any] | None = None,
) -> str:
    status = str(table.get("status") or "pending_review")
    status_label = status.replace("_", " ").title()
    schema = table.get("schema") or []
    profiling = table.get("profiling") or {}
    relationships = table.get("relationships") or []
    notes = table.get("notes") or []
    approve_btn = ""
    header_reject = ""
    reload_mode = bool(table.get("reload_mode"))
    reload_validation = str(table.get("reload_validation_status") or "")
    if readonly:
        meta = catalog_meta or {}
        approved_at = str(meta.get("approved_at") or table.get("approved_at") or "")
        approved_by = str(meta.get("approved_by") or table.get("approved_by") or "")
        workbook = str(meta.get("filename") or "")
        details = []
        if workbook:
            details.append(f"Workbook: {escape(workbook)}")
        if approved_at:
            details.append(f"Approved {escape(approved_at)}")
        if approved_by:
            details.append(f"by {escape(approved_by)}")
        detail_text = " · ".join(details) if details else "Approved proposal"
        approve_btn = f'<p class="muted">{detail_text}</p>'
    elif reload_mode and reload_validation == "passed":
        approve_btn = ""
    elif reload_mode and reload_validation == "failed":
        approve_btn = ""
    elif status != "approved":
        transformation = table.get("transformation") or {}
        steps = transformation.get("steps") or []
        transform_status = str(table.get("transformation_status") or "")
        shape_status = str(table.get("clean_shape_status") or "")
        has_clean_goal = bool(table.get("clean_goal"))
        shape_ok = (not has_clean_goal) or shape_status == "approved"
        can_approve_table = shape_ok and (not steps or transform_status == "approved")
        table_id = str(table.get("table_id") or "")
        if can_approve_table:
            hint = '<p class="muted">Approve to catalog this table into silver, then review DNA join proposals.</p>'
            approve_btn = f"""
        <form method="post" class="assistant-approve-form">
          <input type="hidden" name="action" value="approve_table" />
          <input type="hidden" name="job_id" value="{escape(job_id)}" />
          <input type="hidden" name="table_id" value="{escape(table_id)}" />
          <input type="hidden" name="table_index" value="{table_index}" />
          <button type="submit" class="btn btn-primary">Approve table</button>
        </form>
        {hint}
        """
        else:
            approve_btn = f"""
        <form method="post" class="assistant-approve-form">
          <input type="hidden" name="action" value="approve_table" />
          <input type="hidden" name="job_id" value="{escape(job_id)}" />
          <input type="hidden" name="table_id" value="{escape(table_id)}" />
          <input type="hidden" name="table_index" value="{table_index}" />
          <button type="submit" class="btn btn-primary" disabled>Approve table</button>
        </form>
        """
            if has_clean_goal and shape_status != "approved":
                approve_btn += '<p class="muted">Approve the cleaned shape before approving the table.</p>'
            else:
                approve_btn += '<p class="muted">Approve the transformation before approving the table.</p>'
    else:
        approve_btn = ""

    table_id = str(table.get("table_id") or "")
    if (
        not readonly
        and not reload_mode
        and job_id
        and table_id
        and status != "approved"
    ):
        header_reject = f"""
        <form method="post" class="spreadsheet-table-head-reject">
          <input type="hidden" name="action" value="reject_table" />
          <input type="hidden" name="job_id" value="{escape(job_id)}" />
          <input type="hidden" name="table_id" value="{escape(table_id)}" />
          <input type="hidden" name="table_index" value="{table_index}" />
          <button type="submit" class="btn btn-secondary" formnovalidate>Reject</button>
        </form>
        """

    prev_href = next_href = ""
    nav = ""
    if not readonly and url and job_id:
        if table_index > 0:
            prev_href = _proposal_url(url, source=source, job_id=job_id, table_index=table_index - 1)
        if table_index < total - 1:
            next_href = _proposal_url(url, source=source, job_id=job_id, table_index=table_index + 1)
        nav = '<div class="assistant-diff-nav">'
        nav += f'<span class="assistant-diff-nav-label">Table {table_index + 1} of {total}</span>'
        if prev_href:
            nav += f'<a class="btn btn-secondary assistant-diff-nav-btn" href="{escape(prev_href)}">Previous</a>'
        if next_href:
            nav += f'<a class="btn btn-secondary assistant-diff-nav-btn" href="{escape(next_href)}">Next</a>'
        nav += f'<span class="kpi-chip">{escape(status_label)}</span></div>'

    show_transform = bool((table.get("transformation") or {}).get("steps")) or (
        str(table.get("clean_shape_status") or "") == "approved"
    )
    transform_block = ""
    if show_transform:
        transform_block = _transformation_panel_html(
            table,
            job_id=job_id,
            table_index=table_index,
            url=url,
            source=source,
            readonly=readonly or reload_mode,
            transform_preview=transform_preview,
        )

    inner = f"""
      {_reload_validation_html(table, job_id=job_id, table_index=table_index, url=url, source=source)}
      {_pipeline_stage_stepper_html(table)}
      <p class="pack-card-lead">{escape(str(table.get('purpose') or ''))}</p>
      <div class="spreadsheet-preview-stack">
        <section class="spreadsheet-transform-panel spreadsheet-source-preview-panel">
          <div class="spreadsheet-transform-head">
            <h3 class="kpi-section-heading">Source data preview</h3>
          </div>
          {_preview_html(table_preview, heading="", max_rows=8, compact=True) or '<p class="muted">No source preview.</p>'}
        </section>
        {_clean_shape_panel_html(
            table,
            job_id=job_id,
            table_index=table_index,
            url=url,
            source=source,
            readonly=readonly or reload_mode,
        )}
      </div>
      {_stats_html(table)}
      {transform_block}
      {_schema_profiling_panel_html(schema, profiling)}
      {_relationships_html(relationships)}
      {_join_proposals_panel_html(
          table,
          job_id=job_id,
          table_index=table_index,
          readonly=readonly or reload_mode,
      )}
      {_notes_html(notes)}
      {approve_btn}
    """
    if embedded:
        return inner
    title = escape(str(table.get("entity_name") or table.get("table_id") or "Proposed table"))
    heading = f"<h2>{title}</h2>"
    if header_reject:
        heading = (
            f'<div class="spreadsheet-table-head">{heading}{header_reject}</div>'
        )
    return f"""
    <section class="card pack-card" id="spreadsheet-table-analysis">
      {nav}
      {heading}
      {inner}
    </section>
    """


def _file_pager_html(
    *,
    jobs: list[dict[str, Any]],
    active_job_id: str,
    url: Callable[[str], str],
    source: str,
    is_admin: bool = False,
) -> str:
    if not jobs:
        return ""
    chips = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        chips.append(
            {
                "active": job_id == active_job_id,
                "job_id": job_id,
                "href": _proposal_url(url, source=source, job_id=job_id, table_index=0),
                "name": str(job.get("filename") or job_id or "workbook"),
                "badge": _job_status_short(job),
                "done": str(job.get("status") or "") == "ready",
            }
        )
    return render_template(
        "portal/spreadsheet_engine/_file_chip_nav.html",
        chips=chips,
        nav_class="source-docs-source-nav spreadsheet-file-nav",
        aria_label="Uploaded workbooks",
        is_admin=is_admin,
    )


def _file_summary_html(
    *,
    job: dict[str, Any],
    jobs: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    url: Callable[[str], str],
    source: str,
    is_admin: bool,
) -> str:
    job_id = str(job.get("job_id") or "")
    filename = str(job.get("filename") or "Workbook")
    status = str(job.get("status") or "")
    table_count = len(tables)
    if table_count:
        noun = "table" if table_count == 1 else "tables"
        detail = (
            f"{table_count} proposed {noun} "
            "— review schema, grain, and profiling, then approve or refine with chat."
        )
    elif status == "awaiting_sheets":
        detail = "Select which sheets to analyze for this workbook."
    elif status in _IN_FLIGHT_JOB_STATUSES:
        detail = "AI is generating cleaned table proposals for this workbook."
    elif status == "error":
        detail = str(job.get("error") or "Analysis failed for this workbook.")
    else:
        detail = "No table proposals yet for this workbook."

    job_ids = [str(item.get("job_id") or "") for item in jobs if str(item.get("job_id") or "")]
    file_index = job_ids.index(job_id) if job_id in job_ids else 0
    total_files = len(job_ids)
    prev_href = next_href = ""
    if file_index > 0:
        prev_href = _proposal_url(url, source=source, job_id=job_ids[file_index - 1], table_index=0)
    if file_index < total_files - 1:
        next_href = _proposal_url(url, source=source, job_id=job_ids[file_index + 1], table_index=0)
    nav = '<div class="assistant-diff-nav spreadsheet-file-diff-nav">'
    nav += f'<span class="assistant-diff-nav-label">File {file_index + 1} of {total_files or 1}</span>'
    if prev_href:
        nav += f'<a class="btn btn-secondary assistant-diff-nav-btn" href="{escape(prev_href)}">Previous file</a>'
    if next_href:
        nav += f'<a class="btn btn-secondary assistant-diff-nav-btn" href="{escape(next_href)}">Next file</a>'
    nav += f'<span class="kpi-chip">{escape(_job_status_short(job))}</span></div>'

    reject = ""
    if is_admin and job_id and status != "discarded":
        reject = f"""
        <form method="post" class="spreadsheet-table-head-reject spreadsheet-file-head-reject">
          <input type="hidden" name="action" value="reject_job" />
          <input type="hidden" name="job_id" value="{escape(job_id)}" />
          <button type="submit" class="btn btn-secondary" formnovalidate>Reject file</button>
        </form>
        """
    heading = f"<h2>{escape(filename)}</h2>"
    if reject:
        heading = f'<div class="spreadsheet-table-head">{heading}{reject}</div>'
    return f"""
        <section class="card spreadsheet-job-summary">
          {nav}
          {heading}
          <p class="muted">{escape(detail)}</p>
        </section>
    """


def _table_pager_html(
    *,
    job_id: str,
    tables: list[dict[str, Any]],
    table_index: int,
    url: Callable[[str], str],
    source: str,
) -> str:
    if not tables:
        return ""
    from hiveflow.spreadsheet.stages import STAGE_LABELS, table_pipeline_stage

    chips = []
    for idx, table in enumerate(tables):
        label = str(table.get("entity_name") or table.get("table_id") or f"Table {idx + 1}")
        stage = table_pipeline_stage(table)
        stage_label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())
        # Short badge text for chips
        short = {
            "clean_review": "Clean review",
            "transform_review": "Transform review",
            "transform_approved": "Ready to save",
            "catalogued": "Catalogued",
            "join_review": "Join review",
            "joins_approved": "Joins approved",
            "approved": "Approved",
        }.get(stage, stage_label)
        chips.append(
            {
                "active": idx == table_index,
                "href": _proposal_url(url, source=source, job_id=job_id, table_index=idx),
                "name": label,
                "badge": short,
                "done": stage in {"approved", "joins_approved"},
            }
        )
    return render_template(
        "portal/spreadsheet_engine/_source_chip_nav.html",
        chips=chips,
        nav_class="source-docs-source-nav",
        aria_label="Proposed tables",
    )


def _tabs_html(*, active_tab: str, review_count: int, catalog_count: int) -> str:
    analyze_active = active_tab == "analyze"
    review_active = active_tab == "review"
    catalog_active = active_tab == "catalog"
    review_label = f"Proposals ({review_count})" if review_count else "Proposals"
    catalog_label = f"Catalog ({catalog_count})" if catalog_count else "Catalog"
    return f"""
    <div class="semantic-builder-keys-tabs" role="tablist" aria-label="Spreadsheet Engine">
      <button type="button" class="semantic-builder-keys-tab{" active" if analyze_active else ""}" role="tab"
        data-spreadsheet-tab="analyze" aria-selected="{"true" if analyze_active else "false"}"
        aria-controls="spreadsheet-engine-panel-analyze">Upload</button>
      <button type="button" class="semantic-builder-keys-tab{" active" if review_active else ""}" role="tab"
        data-spreadsheet-tab="review" aria-selected="{"true" if review_active else "false"}"
        aria-controls="spreadsheet-engine-panel-review">{escape(review_label)}</button>
      <button type="button" class="semantic-builder-keys-tab{" active" if catalog_active else ""}" role="tab"
        data-spreadsheet-tab="catalog" aria-selected="{"true" if catalog_active else "false"}"
        aria-controls="spreadsheet-engine-panel-catalog">{escape(catalog_label)}</button>
    </div>
    """


def _upload_form_html(
    url: Callable[[str], str],
    *,
    is_admin: bool,
    source: str,
    catalog_entries: list[dict[str, Any]] | None = None,
    prefill_catalog_id: str = "",
) -> str:
    if not is_admin:
        return '<p class="muted">Ask an admin to upload a workbook for analysis.</p>'
    options = []
    for entry in catalog_entries or []:
        cid = str(entry.get("catalog_id") or "")
        if not cid:
            continue
        options.append(
            {
                "value": cid,
                "label": f"{entry.get('entity_name') or cid} ({entry.get('filename') or ''})",
                "selected": cid == prefill_catalog_id,
            }
        )
    return render_template(
        "portal/spreadsheet_engine/_upload_form.html",
        action_url=url(source_docs_inspector_path(source)),
        options=options,
    )


def _short_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    if "T" in text:
        date, rest = text.split("T", 1)
        clock = rest[:5] if rest else ""
        return f"{date} {clock}".strip()
    return text


def _catalog_section_html(title: str, body: str, *, open_default: bool = False) -> str:
    opened = " open" if open_default else ""
    return (
        f'<details class="spreadsheet-catalog-section"{opened}>'
        f"<summary>{escape(title)}</summary>"
        f'<div class="spreadsheet-catalog-section-body">{body}</div>'
        "</details>"
    )


def _catalog_list_html(
    entries: list[dict[str, Any]],
    *,
    url: Callable[[str], str],
    source: str,
    active_catalog_id: str = "",
) -> str:
    if not entries:
        return render_template("portal/spreadsheet_engine/_catalog_list_empty.html")
    chips = []
    for entry in entries:
        cid = str(entry.get("catalog_id") or "")
        name = str(entry.get("filename") or entry.get("entity_name") or cid or "workbook")
        entity = str(entry.get("entity_name") or "")
        chips.append(
            {
                "active": bool(cid) and cid == active_catalog_id,
                "href": _catalog_url(url, source=source, catalog_id=cid),
                "name": name,
                "entity": entity if entity and entity.lower() not in name.lower() else None,
                "last_upload": _short_timestamp(
                    entry.get("last_upload_at") or entry.get("approved_at") or ""
                ),
            }
        )
    return render_template("portal/spreadsheet_engine/_catalog_list.html", chips=chips)


def _catalog_detail_html(
    entry: dict[str, Any],
    *,
    url: Callable[[str], str],
    source: str,
    table_preview: dict[str, Any] | None = None,
    is_admin: bool = False,
) -> str:
    proposal = entry.get("proposal") if isinstance(entry.get("proposal"), dict) else entry
    if not isinstance(proposal, dict):
        proposal = {}
    entity = str(entry.get("entity_name") or proposal.get("entity_name") or "Approved table")
    catalog_id = str(entry.get("catalog_id") or "")
    filename = str(entry.get("filename") or "workbook")
    transformation = entry.get("transformation") or proposal.get("transformation") or {}
    output_shape = entry.get("output_shape") or transformation.get("output_shape") or {}
    schema = (
        list(proposal.get("schema") or [])
        or list(output_shape.get("schema") or [])
    )
    clean_goal = proposal.get("clean_goal") or {}
    preview_payload = None
    if isinstance(clean_goal, dict) and (clean_goal.get("rows") or clean_goal.get("headers")):
        preview_payload = {
            "headers": list(clean_goal.get("headers") or []),
            "rows": list(clean_goal.get("rows") or []),
            "row_count": int(clean_goal.get("row_count") or len(clean_goal.get("rows") or [])),
            "preview_row_count": int(
                clean_goal.get("preview_row_count") or len(clean_goal.get("rows") or [])
            ),
            "truncated": bool(clean_goal.get("truncated")),
        }
    elif table_preview:
        preview_payload = table_preview

    silver_key = str(entry.get("silver_parquet_key") or entry.get("silver_parquet_location") or "")
    silver_rows = entry.get("silver_row_count")
    workbook_value = f"<strong>{escape(filename)}</strong>"
    if catalog_id:
        download_href = escape(
            url(f"/api/spreadsheet-engine/workbook?catalog_id={catalog_id}")
        )
        workbook_value += (
            f' <a class="spreadsheet-catalog-download" href="{download_href}" '
            f'download="{escape(filename)}">Download</a>'
        )
    output_bits = [
        f"<div class=\"spreadsheet-catalog-output-row\"><span>Entity</span><strong>{escape(entity)}</strong></div>",
        f"<div class=\"spreadsheet-catalog-output-row\"><span>Workbook</span>"
        f"<span class=\"spreadsheet-catalog-workbook-value\">{workbook_value}</span></div>",
    ]
    grain = str(proposal.get("grain") or output_shape.get("grain") or "")
    if grain:
        output_bits.append(
            f"<div class=\"spreadsheet-catalog-output-row\"><span>Grain</span><strong>{escape(grain)}</strong></div>"
        )
    if silver_key:
        output_bits.append(
            f"<div class=\"spreadsheet-catalog-output-row\"><span>Output file</span>"
            f"<code>{escape(silver_key)}</code></div>"
        )
    if silver_rows is not None and str(silver_rows).strip() != "":
        output_bits.append(
            f"<div class=\"spreadsheet-catalog-output-row\"><span>Rows</span>"
            f"<strong>{escape(str(silver_rows))}</strong></div>"
        )
    purpose = str(proposal.get("purpose") or "").strip()
    if purpose:
        output_bits.append(f"<p class=\"muted spreadsheet-catalog-purpose\">{escape(purpose)}</p>")
    output_html = f'<div class="spreadsheet-catalog-output">{"".join(output_bits)}</div>'

    preview_html = _preview_html(preview_payload, heading="", compact=True) or (
        '<p class="muted">No output preview is stored for this catalog entry.</p>'
    )
    schema_html = (
        '<div class="table-wrap spreadsheet-preview-table">'
        f"{_schema_table_html(schema)}"
        "</div>"
    )
    transform_html = _transformation_steps_html(transformation)

    reupload_form = ""
    if is_admin and catalog_id:
        reupload_form = f"""
        <form method="post" enctype="multipart/form-data" class="spreadsheet-reupload-form">
          <input type="hidden" name="action" value="reupload_catalog" />
          <input type="hidden" name="catalog_id" value="{escape(catalog_id)}" />
          <label class="spreadsheet-dropzone spreadsheet-reupload-dropzone" for="spreadsheet-reupload-workbook">
            <span class="spreadsheet-dropzone-title">Re-upload workbook for {escape(filename)}</span>
            <span class="spreadsheet-dropzone-hint muted">Applies the approved transformation for final approval.</span>
          </label>
          <input type="file" name="workbook" id="spreadsheet-reupload-workbook" class="spreadsheet-file-input"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required />
          <button type="submit" class="btn btn-primary portal-submit-btn">Analyze re-upload</button>
        </form>
        """

    return f"""
    <section class="card spreadsheet-catalog-detail">
      <div class="spreadsheet-catalog-detail-head">
        <h2>{escape(filename)}</h2>
        <span class="kpi-chip">Approved</span>
      </div>
      <p class="muted">{escape(entity)}</p>
      {reupload_form}
      {_catalog_section_html("Output file", output_html, open_default=True)}
      {_catalog_section_html("Preview", preview_html, open_default=True)}
      {_catalog_section_html("Schema", schema_html)}
      {_catalog_section_html("Transformation set", transform_html)}
    </section>
    """


def _catalog_tab_html(
    entries: list[dict[str, Any]],
    *,
    url: Callable[[str], str],
    source: str,
    active_catalog: dict[str, Any] | None = None,
    table_preview: dict[str, Any] | None = None,
    is_admin: bool = False,
) -> str:
    catalog = list(entries or [])
    selected = active_catalog
    if not selected and catalog:
        selected = catalog[0]
    active_id = str((selected or {}).get("catalog_id") or "")
    if not catalog:
        return _catalog_list_html([], url=url, source=source)
    detail = ""
    if selected:
        detail = _catalog_detail_html(
            selected,
            url=url,
            source=source,
            table_preview=table_preview,
            is_admin=is_admin,
        )
    return f"""
    <div class="spreadsheet-catalog-layout">
      {_catalog_list_html(catalog, url=url, source=source, active_catalog_id=active_id)}
      {detail}
    </div>
    """


def _chat_panel_html(
    url: Callable[[str], str],
    *,
    source: str,
    job_id: str,
    table: dict[str, Any] | None,
    table_index: int,
    disabled: bool = False,
) -> str:
    table_id = str((table or {}).get("table_id") or "")
    entity_name = str((table or {}).get("entity_name") or table_id or "this table")
    if disabled or not table_id:
        return ""
    shape_rejected = str((table or {}).get("clean_shape_status") or "") == "rejected"
    transform_rejected = str((table or {}).get("transformation_status") or "") == "rejected"
    table_rejected = str((table or {}).get("status") or "") == "rejected"
    history = list((table or {}).get("chat_history") or [])
    if not (shape_rejected or transform_rejected or table_rejected or history):
        return ""
    return f"""
    <section class="card" id="spreadsheet-table-chat">
      <h2>Chat history</h2>
      <p class="muted">Feedback applies to <strong>{escape(entity_name)}</strong>. The assistant already has the current proposal (cleaned data, schema, and transformation).</p>
      <div class="governance-update-panel">
        <div class="assistant-chat-shell">
          <div class="assistant-chat">
            {_chat_html(table, entity_name=entity_name)}
          </div>
          <form method="post" action="{escape(url(source_docs_inspector_path(source)))}" class="assistant-compose">
            <input type="hidden" name="action" value="chat" />
            <input type="hidden" name="job_id" value="{escape(job_id)}" />
            <input type="hidden" name="table_id" value="{escape(table_id)}" />
            <input type="hidden" name="table_index" value="{table_index}" />
            <div class="form-field assistant-compose-field">
              <label for="spreadsheet-chat">Message</label>
              <textarea id="spreadsheet-chat" name="message" rows="2" required
                class="assistant-compose-input"
                placeholder="e.g. Treat Customer No as the primary key and rename it customer_id"></textarea>
            </div>
            <button type="submit" class="btn btn-primary portal-submit-btn">Send</button>
          </form>
        </div>
      </div>
    </section>
    """


def _sheet_selection_html(
    *,
    job_id: str,
    filename: str,
    sheets: list[dict[str, Any]],
    sheet_names: list[str],
) -> str:
    names = [str(item).strip() for item in sheet_names if str(item).strip()]
    if not names and sheets:
        names = [str(item.get("name") or "").strip() for item in sheets if str(item.get("name") or "").strip()]
    count_by_name = {
        str(item.get("name") or ""): int(item.get("table_count") or 0)
        for item in sheets
        if isinstance(item, dict)
    }
    rows = [{"name": name, "table_count": count_by_name.get(name)} for name in names]
    return render_template(
        "portal/spreadsheet_engine/_sheet_selection.html",
        job_id=job_id,
        filename=filename or "workbook",
        rows=rows,
    )


def _proposal_generation_status_html(
    *,
    filename: str,
    pipeline: dict[str, Any],
    job_id: str,
) -> str:
    error = str(pipeline.get("error") or "").strip()
    error_html = ""
    if error:
        error_html = f'<p class="form-error spreadsheet-proposal-status-error" id="spreadsheet-proposal-status-error">{escape(error)}</p>'
    default_label = "Generating cleaned proposals"
    default_detail = "AI is generating a cleaned proposal of all tables in this workbook."
    return f"""
    <section class="card spreadsheet-proposal-status" id="spreadsheet-proposal-status"
             data-job-id="{escape(job_id)}">
      <div class="spreadsheet-proposal-status-head">
        <div>
          <h2 id="spreadsheet-proposal-status-label">{escape(str(pipeline.get("status_label") or default_label))}</h2>
          <p class="muted" id="spreadsheet-proposal-status-detail">{escape(str(pipeline.get("status_detail") or default_detail))}</p>
        </div>
      </div>
      <p class="muted spreadsheet-proposal-status-workbook">Workbook: <strong>{escape(filename or "workbook")}</strong></p>
      {error_html}
    </section>
    """


def _proposal_finished_empty_html(*, filename: str, error: str = "") -> str:
    if error:
        body = f'<p class="form-error">{escape(error)}</p>'
    else:
        body = (
            '<p class="pack-card-lead">Analysis finished but no table proposals were generated. '
            "Try uploading a workbook with a clear header row and tabular data.</p>"
        )
    return f"""
    <section class="card pack-card spreadsheet-proposal-empty">
      <h2>{escape(filename or "Workbook")}</h2>
      {body}
    </section>
    """


def _ready_banner_html(
    *,
    url: Callable[[str], str],
    source: str,
    job_id: str,
    table_count: int,
) -> str:
    href = _proposal_url(url, source=source, job_id=job_id, table_index=0)
    noun = "table" if table_count == 1 else "tables"
    return f"""
    <div class="form-success spreadsheet-ready-banner">
      Analysis complete — {table_count} proposed {noun} ready for review.
      <a class="btn btn-secondary spreadsheet-ready-banner-btn" href="{escape(href)}">View proposals</a>
    </div>
    """


def _tabs_script() -> str:
    return """
<script>
(function () {
  function activateTab(name) {
    var section = document.getElementById("spreadsheet-engine-tabs");
    if (!section) return;
    section.querySelectorAll("[data-spreadsheet-tab]").forEach(function (tab) {
      var active = tab.getAttribute("data-spreadsheet-tab") === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    section.querySelectorAll("[data-spreadsheet-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-spreadsheet-panel") !== name;
    });
  }
  function syncTabUrl(name) {
    var url = new URL(window.location.href);
    url.searchParams.set("tab", name);
    window.history.replaceState({}, "", url.toString());
  }
  var section = document.getElementById("spreadsheet-engine-tabs");
  if (!section) return;
  var defaultTab = section.getAttribute("data-default-tab") || "analyze";
  activateTab(defaultTab);
  section.querySelectorAll("[data-spreadsheet-tab]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      var name = tab.getAttribute("data-spreadsheet-tab") || "analyze";
      activateTab(name);
      syncTabUrl(name);
    });
  });
})();
</script>
"""


def _compose_script() -> str:
    return """
<script>
(function () {
  var box = document.getElementById("spreadsheet-chat");
  var form = document.querySelector("#spreadsheet-table-chat form.assistant-compose");
  if (!box || !form || box.dataset.enterBound === "1") return;
  box.dataset.enterBound = "1";
  box.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
})();
</script>
"""


def _scroll_script() -> str:
    return """
<script>
(function () {
  var chat = document.querySelector("#spreadsheet-table-chat .assistant-chat");
  if (!chat) return;
  chat.scrollTop = chat.scrollHeight;
})();
</script>
"""


def _status_poll_script(status_url: str, job_id: str, *, poll: bool) -> str:
    if not job_id or not poll:
        return ""
    return f"""
<script>
(function () {{
  var statusUrl = {_json_for_script(status_url)};
  var jobId = {_json_for_script(job_id)};
  var statusRoot = document.getElementById("spreadsheet-proposal-status");
  if (!statusRoot) return;

  var stopped = false;
  var reloadKey = "sse-proposals-loaded-" + jobId;

  function hasProposalContent() {{
    return !!document.getElementById("spreadsheet-table-analysis");
  }}

  function renderPipeline(pipeline) {{
    if (!pipeline) return;
    var label = document.getElementById("spreadsheet-proposal-status-label");
    var detail = document.getElementById("spreadsheet-proposal-status-detail");
    var error = document.getElementById("spreadsheet-proposal-status-error");
    if (label) label.textContent = pipeline.status_label || "Generating cleaned proposals";
    if (detail) detail.textContent = pipeline.status_detail || "AI is generating a cleaned proposal of all tables in this workbook.";
    if (error) {{
      if (pipeline.error) {{
        error.textContent = pipeline.error;
        error.hidden = false;
      }} else {{
        error.hidden = true;
      }}
    }}
  }}

  function tableCount(payload) {{
    if (payload.report && payload.report.tables && payload.report.tables.length) {{
      return payload.report.tables.length;
    }}
    return payload.table_count || 0;
  }}

  function stopPolling() {{
    stopped = true;
  }}

  function handlePayload(payload) {{
    if (stopped || hasProposalContent()) {{
      stopPolling();
      return true;
    }}
    if (payload.pipeline) renderPipeline(payload.pipeline);
    var status = payload.status || "";
    var tablesReady = tableCount(payload) > 0 && (status === "ready" || status === "error");
    if (!tablesReady && tableCount(payload) > 0 && payload.report && payload.report.tables) {{
      tablesReady = payload.report.tables.every(function (table) {{
        return table && table.clean_goal;
      }}) && status !== "error";
    }}
    if (tablesReady) {{
      stopPolling();
      if (sessionStorage.getItem(reloadKey) === "1") {{
        return true;
      }}
      sessionStorage.setItem(reloadKey, "1");
      var url = new URL(window.location.href);
      url.searchParams.set("job_id", jobId);
      url.searchParams.set("tab", "review");
      url.searchParams.set("table_index", "0");
      window.location.replace(url.toString());
      return true;
    }}
    if (payload.pipeline && payload.pipeline.failed) {{
      stopPolling();
      return true;
    }}
    if (payload.status === "ready" || payload.status === "error") {{
      stopPolling();
      if (sessionStorage.getItem(reloadKey) !== "1") {{
        sessionStorage.setItem(reloadKey, "1");
        window.location.replace(window.location.href);
      }}
      return true;
    }}
    return false;
  }}

  function pollOnce() {{
    if (stopped) return Promise.resolve(true);
    return fetch(statusUrl + "?job_id=" + encodeURIComponent(jobId), {{
      credentials: "same-origin",
      headers: {{ "Accept": "application/json" }}
    }})
      .then(function (r) {{ return r.json(); }})
      .then(function (payload) {{ return handlePayload(payload); }})
      .catch(function () {{ return false; }});
  }}

  pollOnce().then(function (done) {{
    if (done || stopped) return;
    var timer = setInterval(function () {{
      pollOnce().then(function (finished) {{
        if (finished) clearInterval(timer);
      }});
    }}, 2500);
  }});
}})();
</script>
"""


def _dropzone_script() -> str:
    return """
<script>
(function () {
  var zone = document.getElementById("spreadsheet-dropzone");
  var input = document.getElementById("spreadsheet-workbook");
  if (!zone || !input) return;
  function setName() {
    var label = zone.querySelector(".spreadsheet-dropzone-title");
    if (!label || !input.files || !input.files.length) return;
    label.textContent = input.files[0].name;
  }
  input.addEventListener("change", setName);
  ["dragenter", "dragover"].forEach(function (name) {
    zone.addEventListener(name, function (event) {
      event.preventDefault();
      zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    zone.addEventListener(name, function (event) {
      event.preventDefault();
      zone.classList.remove("is-dragover");
    });
  });
  zone.addEventListener("drop", function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    if (!files || !files.length) return;
    input.files = files;
    setName();
  });
})();
</script>
"""


def render_spreadsheet_engine_page(
    *,
    url: Callable[[str], str],
    sources: list[str],
    active_source: str,
    availability: dict[str, bool],
    is_admin: bool,
    job: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    request_job_id: str = "",
    table_index: int = 0,
    catalog_entries: list[dict[str, Any]] | None = None,
    active_catalog: dict[str, Any] | None = None,
    message: str = "",
    error: str = "",
    status_url: str = "",
    active_tab: str = "analyze",
    table_preview: dict[str, Any] | None = None,
    catalog_preview: dict[str, Any] | None = None,
    transform_preview: dict[str, Any] | None = None,
    prefill_catalog_id: str = "",
    proposal_jobs: list[dict[str, Any]] | None = None,
) -> str:
    source = normalize_reference_source(active_source) or "sse"
    job_id = str((job or {}).get("job_id") or request_job_id or "")
    job_status = str((job or {}).get("status") or "")
    filename = str((job or {}).get("filename") or "")
    jobs = [
        item
        for item in (proposal_jobs or [])
        if isinstance(item, dict) and str(item.get("status") or "") != "discarded"
    ]
    if job and job_id and all(str(item.get("job_id") or "") != job_id for item in jobs):
        if job_status != "discarded":
            jobs = [job, *jobs]
    tables = _active_proposal_tables(list((report or {}).get("tables") or []))
    analyzing = job_status in _IN_FLIGHT_JOB_STATUSES
    awaiting_sheets = job_status == "awaiting_sheets" or (
        bool(job_id)
        and not analyzing
        and job_status not in {"ready", "error"}
        and bool((job or {}).get("sheet_names") or (job or {}).get("sheets"))
        and not (job or {}).get("selected_sheets")
        and not tables
    )
    proposals_ready = job_status == "ready" or (
        bool(tables)
        and job_status not in _IN_FLIGHT_JOB_STATUSES
        and job_status != "error"
    )
    has_proposals = proposals_ready and bool(tables)
    show_generation_status = bool(job_id) and not has_proposals and not awaiting_sheets and (
        analyzing or (bool(job_id) and job_status not in {"error", "ready", "awaiting_sheets"} and not tables)
    )
    replacing_approved = bool(
        (job or {}).get("reload_mode")
        or (job or {}).get("reupload")
        or (job or {}).get("linked_catalog_id")
    )
    pipeline = spreadsheet_pipeline_progress(
        job_status or ("running" if job_id and analyzing else ""),
        error=str((job or {}).get("error") or ""),
        reload_mode=replacing_approved,
    )
    catalog = list(catalog_entries or [])
    if table_index < 0 or table_index >= len(tables):
        table_index = 0
    active_table = tables[table_index] if tables else None

    if active_tab == "catalog":
        tab = "catalog"
    elif active_tab == "analyze":
        tab = "analyze"
    elif active_tab == "review" or has_proposals or awaiting_sheets or job_id or jobs:
        tab = "review"
    else:
        tab = "analyze"

    body = f"""
    <div class="source-docs-page spreadsheet-engine-page" data-source="{escape(source)}">
      {_source_switcher(
          sources=sources,
          active_source=source,
          url=url,
          availability=availability,
      )}
    """
    if message:
        body += f'<div class="form-success">{escape(message)}</div>'
    if error:
        body += f'<div class="form-error">{escape(error)}</div>'
    if job_id and job_status == "error":
        body += f'<div class="form-error">{escape(str((job or {}).get("error") or "Analysis failed."))}</div>'
    if has_proposals and tab == "analyze":
        body += _ready_banner_html(
            url=url, source=source, job_id=job_id, table_count=len(tables)
        )

    analyze_hidden = "" if tab == "analyze" else " hidden"
    review_hidden = "" if tab == "review" else " hidden"
    catalog_hidden = "" if tab == "catalog" else " hidden"

    body += f"""
    <section class="semantic-builder-keys-tabs-section" id="spreadsheet-engine-tabs"
             data-default-tab="{escape(tab)}">
      {_tabs_html(active_tab=tab, review_count=len(jobs), catalog_count=len(catalog))}
      <div class="semantic-builder-keys-panel" id="spreadsheet-engine-panel-analyze"
           data-spreadsheet-panel="analyze" role="tabpanel"{analyze_hidden}>
        <section class="card" id="spreadsheet-engine-upload">
          <h2>Upload workbook</h2>
          <p class="muted">Excel workbooks are parsed into sheets first. Select which sheets to analyze, then review proposed tables.</p>
          {_upload_form_html(
              url,
              is_admin=is_admin,
              source=source,
              catalog_entries=catalog,
              prefill_catalog_id=prefill_catalog_id,
          )}
        </section>
    """

    body += f"""
      </div>
      <div class="semantic-builder-keys-panel" id="spreadsheet-engine-panel-review"
           data-spreadsheet-panel="review" role="tabpanel"{review_hidden}>
"""

    if jobs:
        body += _file_pager_html(
            jobs=jobs,
            active_job_id=job_id,
            url=url,
            source=source,
            is_admin=is_admin,
        )
    if job:
        body += _file_summary_html(
            job=job,
            jobs=jobs or [job],
            tables=tables,
            url=url,
            source=source,
            is_admin=is_admin,
        )

    if awaiting_sheets:
        body += _sheet_selection_html(
            job_id=job_id,
            filename=filename,
            sheets=list((job or {}).get("sheets") or []),
            sheet_names=list((job or {}).get("sheet_names") or []),
        )
    elif show_generation_status:
        body += _proposal_generation_status_html(
            filename=filename,
            pipeline=pipeline,
            job_id=job_id,
        )
    elif has_proposals:
        suggested = list((job or {}).get("suggested_catalog_ids") or [])
        linked = str((job or {}).get("linked_catalog_id") or "")
        if suggested and not linked and is_admin:
            links = ""
            for cid in suggested[:3]:
                links += f"""
                <form method="post" class="spreadsheet-catalog-suggest-form" style="display:inline">
                  <input type="hidden" name="action" value="link_catalog" />
                  <input type="hidden" name="job_id" value="{escape(job_id)}" />
                  <input type="hidden" name="catalog_id" value="{escape(cid)}" />
                  <button type="submit" class="btn btn-secondary btn-sm">{escape(cid)}</button>
                </form>
                """
            body += f"""
        <section class="card spreadsheet-catalog-suggestions">
          <h2>Catalog match suggestions</h2>
          <p class="muted">This workbook structure matches existing catalog entries. Link one to reuse transformations:</p>
          <div class="spreadsheet-catalog-suggest-actions">{links}</div>
        </section>
            """
        body += _table_pager_html(
            job_id=job_id,
            tables=tables,
            table_index=table_index,
            url=url,
            source=source,
        )
        body += _table_analysis_html(
            active_table or {},
            job_id=job_id,
            table_index=table_index,
            total=len(tables),
            url=url,
            source=source,
            table_preview=table_preview,
            transform_preview=transform_preview,
        )
        body += _chat_panel_html(
            url,
            source=source,
            job_id=job_id,
            table=active_table,
            table_index=table_index,
        )
    elif job_id and job_status == "error":
        body += _proposal_finished_empty_html(
            filename=filename,
            error=str((job or {}).get("error") or "Analysis failed."),
        )
    elif job_id and job_status == "ready":
        body += _proposal_finished_empty_html(filename=filename)
    elif not jobs:
        body += """
        <section class="card pack-card">
          <h2>Proposals</h2>
          <p class="pack-card-lead">Upload and analyze a workbook on the Upload tab. Each upload stays here until you reject the file. Proposed tables appear under the selected workbook.</p>
        </section>
        """

    body += f"""
      </div>
      <div class="semantic-builder-keys-panel" id="spreadsheet-engine-panel-catalog"
           data-spreadsheet-panel="catalog" role="tabpanel"{catalog_hidden}>
        {_catalog_tab_html(
            catalog,
            url=url,
            source=source,
            active_catalog=active_catalog,
            table_preview=catalog_preview,
            is_admin=is_admin,
        )}
      </div>
    </section>
    """
    body += f'<link rel="stylesheet" href="{escape(url("/static/source-docs-inspector.css"))}" />'
    body += _tabs_script()
    body += _schema_toggle_script()
    body += _compose_script()
    body += _scroll_script()
    body += _dropzone_script()
    body += _status_poll_script(
        status_url,
        job_id,
        poll=show_generation_status,
    )
    body += "</div>"
    return body
