"""Gold source-docs inspector — parallel to Semantic Builder, with overlay edits."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Callable

from markupsafe import Markup
from werkzeug.wrappers import Request, Response

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.source_docs.overlays import list_versions
from hiveflow.dna.source_docs.reference import (
    list_reference_sources,
    load_source_docs_gold,
    normalize_reference_source,
    source_supports_gold_build,
)
from hiveflow.dna.web.portal.dna_nav import (
    SOURCE_DOCS_INSPECTOR_ROOT,
    source_docs_inspector_path,
    source_label,
)
from hiveflow.dna.web.routing_helpers import _app_url
from hiveflow.dna.web.templating import render_template
from hiveflow.dna.web.theme import page_header


def _url(request: Request) -> Callable[[str], str]:
    return lambda path: _app_url(request, path)


def _json_for_script(payload: Any) -> str:
    """Serialize JSON for inline <script> without closing the HTML script element."""
    return json.dumps(payload).replace("<", "\\u003c")


def _catalog_tables(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Prefer `tables`; accept legacy `entities` for older gold files."""
    rows = catalog.get("tables")
    if rows is None:
        rows = catalog.get("entities")
    return [item for item in (rows or []) if isinstance(item, dict)]


def _table_name(row: dict[str, Any]) -> str:
    return str(row.get("silver_entity") or row.get("table") or "").strip()


def _column_name(item: dict[str, Any]) -> str:
    return str(item.get("silver_column") or item.get("name") or item.get("FK") or "").strip()


def _silver_field_meta(item: dict[str, Any]) -> str:
    parts: list[str] = []
    if item.get("in_silver") is False:
        parts.append("not in silver")
    origin = str(item.get("origin") or "").strip()
    if origin:
        parts.append(origin)
    if not parts:
        return ""
    return f'<span class="muted"> · {escape(" · ".join(parts))}</span>'


def _action_btn(
    *,
    label: str,
    kind: str,
    attrs: dict[str, str],
    extra_class: str = "",
    aria_label: str = "",
) -> str:
    classes = "source-docs-edit-btn"
    if extra_class:
        classes += f" {extra_class}"
    data = " ".join(
        f'data-{escape(key)}="{escape(value)}"' for key, value in attrs.items() if value is not None
    )
    aria = f' aria-label="{escape(aria_label)}"' if aria_label else ""
    return (
        f'<button type="button" class="{classes}" data-action="exclude" '
        f'data-kind="{escape(kind)}" {data}{aria}>{escape(label)}</button>'
    )


def _source_switcher(
    *,
    sources: list[str],
    active_source: str,
    url: Callable[[str], str],
    availability: dict[str, bool],
) -> str:
    if len(sources) <= 1:
        chips = [
            {
                "href": url(source_docs_inspector_path(active_source)),
                "label": source_label(active_source),
                "active": True,
                "badge_text": None,
                "badge_empty": False,
            }
        ]
    else:
        chips = []
        for source in sources:
            ready = availability.get(source)
            chips.append(
                {
                    "href": url(source_docs_inspector_path(source)),
                    "label": source_label(source),
                    "active": source == active_source,
                    "badge_text": "Ready" if ready is True else ("Empty" if ready is False else None),
                    "badge_empty": ready is False,
                }
            )
    return render_template("portal/semantics/_source_switcher.html", chips=chips)


def _admin_nav(
    *,
    available: bool,
    is_admin: bool,
    build_supported: bool,
    source: str,
    pending_count: int = 0,
) -> str:
    if not is_admin or not build_supported or not available:
        return ""
    return render_template(
        "portal/semantics/_admin_nav.html", source=source, pending_count=pending_count
    )


def _summary_cards(
    summary: dict[str, Any],
    *,
    available: bool,
    complete: bool,
    silver_profile_present: bool = False,
    silver_reconciled: bool = False,
    silver_profile_key: str = "",
) -> str:
    if not available:
        return ""
    status = "Complete" if complete else "Partial"
    items = [
        ("Tables", summary.get("table_count") or 0),
        ("Columns", summary.get("property_count") or 0),
        ("Relationships", summary.get("relationship_count") or 0),
        ("Tagged columns", summary.get("tagged_property_count") or 0),
        ("Gold status", status),
    ]
    if silver_profile_present:
        items.extend(
            [
                ("Silver tables", summary.get("silver_table_count") or 0),
                ("Columns in silver", summary.get("silver_column_count") or 0),
                (
                    "Silver sync",
                    "Reconciled" if silver_reconciled else "Profile only",
                ),
            ]
        )
    generated = str(summary.get("generated_at") or "").strip()
    profile_at = str(summary.get("silver_profile_generated_at") or "").strip()
    meta_lines: list[Markup] = []
    if generated:
        meta_lines.append(Markup(f"Gold generated at {escape(generated)}"))
    if profile_at:
        meta_lines.append(Markup(f"Silver profile at {escape(profile_at)}"))
    if silver_profile_key:
        meta_lines.append(Markup(f"<code>{escape(silver_profile_key)}</code>"))
    reconcile_note = silver_profile_present and not silver_reconciled
    return render_template(
        "portal/semantics/_summary_cards.html",
        items=[{"label": label, "value": value} for label, value in items],
        meta_lines=meta_lines,
        reconcile_note=reconcile_note,
    )


def _empty_state(*, is_admin: bool, source: str, build_supported: bool) -> str:
    return render_template(
        "portal/semantics/_empty_state.html",
        label=source_label(source),
        source=source,
        is_admin=is_admin,
        build_supported=build_supported,
    )


def _tables_panel(
    catalog: dict[str, Any] | None,
    *,
    is_admin: bool,
    pending_tables: set[str],
) -> str:
    if not catalog:
        return '<p class="semantic-builder-empty-state">entity_properties.yaml is not in gold yet.</p>'
    tables = _catalog_tables(catalog)
    if not tables:
        return '<p class="semantic-builder-empty-state">No tables in gold catalog.</p>'

    ranked = sorted(
        tables,
        key=lambda row: (
            -len([p for p in (row.get("properties") or []) if isinstance(p, dict)]),
            _table_name(row),
        ),
    )
    entities = []
    for table in ranked:
        silver = _table_name(table)
        if not silver:
            continue
        props = [p for p in (table.get("properties") or []) if isinstance(p, dict)]
        rows = [
            {
                "column": _column_name(prop),
                "type": str(prop.get("type") or ""),
                "description": str(prop.get("description") or ""),
                "silver_meta": Markup(_silver_field_meta(prop)),
            }
            for prop in props
        ]
        edit = (
            Markup(_action_btn(label="Remove", kind="table", attrs={"table": silver}))
            if is_admin
            else None
        )
        entities.append(
            {
                "silver": silver,
                "column_count": len(props),
                "not_in_silver": table.get("in_silver") is False,
                "description": str(table.get("description") or ""),
                "edit": edit,
                "rows": rows,
            }
        )

    return render_template(
        "portal/semantics/_tables_panel.html", entities=entities, total_count=len(ranked)
    )


def _relationships_panel(
    catalog: dict[str, Any] | None,
    *,
    is_admin: bool,
    pending_relationships: set[str],
) -> str:
    if not catalog:
        return (
            '<p class="semantic-builder-empty-state">'
            "entity_relationships.yaml is not in gold yet.</p>"
        )
    tables = catalog.get("tables") or {}
    if not isinstance(tables, dict) or not tables:
        return '<p class="semantic-builder-empty-state">No tables in gold relationships catalog.</p>'

    ranked: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for table_name, table in tables.items():
        if not isinstance(table, dict):
            continue
        rels = [r for r in (table.get("relationships") or []) if isinstance(r, dict)]
        ranked.append((str(table_name), table, rels))
    ranked.sort(key=lambda item: (-len(item[2]), item[0]))

    entities = []
    for table_name, table, rels in ranked:
        pk = str(table.get("silver_PK") or table.get("PK") or "")
        rows = []
        for rel in rels:
            fk = str(rel.get("silver_FK") or rel.get("FK") or "")
            target = str(rel.get("target") or "")
            target_pk = str(rel.get("silver_PK") or rel.get("PK") or pk)
            edit = (
                Markup(
                    _action_btn(
                        label="Remove",
                        kind="relationship",
                        attrs={"table": table_name, "fk": fk, "target": target},
                    )
                )
                if is_admin
                else None
            )
            rows.append(
                {
                    "fk": fk,
                    "target": target,
                    "target_pk": target_pk,
                    "silver_meta": Markup(_silver_field_meta(rel)),
                    "edit": edit,
                }
            )
        entities.append(
            {"table_name": table_name, "pk": pk, "rel_count": len(rels), "rows": rows}
        )

    return render_template(
        "portal/semantics/_relationships_panel.html",
        entities=entities,
        total_count=len(ranked),
        is_admin=is_admin,
        relationship_count=int(catalog.get("relationship_count") or 0),
    )

def _collect_all_tags(tables: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for table in tables:
        for prop in table.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            for raw in prop.get("tags") or []:
                tag = str(raw or "").strip()
                key = tag.casefold()
                if not tag or key in seen:
                    continue
                seen.add(key)
                tags.append(tag)
    tags.sort(key=str.casefold)
    return tags


def _tags_panel(
    catalog: dict[str, Any] | None,
    *,
    is_admin: bool,
    pending_tags: set[str],
) -> str:
    if not catalog:
        return (
            '<p class="semantic-builder-empty-state">'
            "entity_property_tags.yaml is not in gold yet.</p>"
        )
    tables = _catalog_tables(catalog)
    if not tables:
        return '<p class="semantic-builder-empty-state">No tables in gold tags catalog.</p>'

    ranked: list[tuple[dict[str, Any], int, list[tuple[str, str, list[str]]]]] = []
    for table in tables:
        rows: list[tuple[str, str, list[str]]] = []
        tag_hits = 0
        for prop in table.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            doc_name = str(prop.get("name") or "").strip()
            if not doc_name:
                continue
            column = _column_name(prop)
            tags = [str(t).strip() for t in (prop.get("tags") or []) if str(t).strip()]
            tag_hits += len(tags)
            rows.append((doc_name, column, tags))
        ranked.append((table, tag_hits, rows))
    ranked.sort(key=lambda item: (-item[1], _table_name(item[0])))

    all_tags = _collect_all_tags(tables)

    entities = []
    for table, tag_hits, rows in ranked:
        silver = _table_name(table)
        if not silver:
            continue
        body_rows = []
        for doc_name, column, tags in rows:
            chips = []
            for tag in tags:
                remove = (
                    Markup(
                        _action_btn(
                            label="×",
                            kind="tag",
                            attrs={"silver-entity": silver, "name": doc_name, "tag": tag},
                            extra_class="source-docs-tag-remove",
                            aria_label=f"Remove tag {tag}",
                        )
                    )
                    if is_admin
                    else None
                )
                chips.append({"tag": tag, "tag_key": tag.casefold(), "remove": remove})
            tag_keys = " ".join(t.casefold() for t in tags)
            body_rows.append({"column": column, "tag_keys": tag_keys, "chips": chips})
        entities.append({"silver": silver, "tag_hits": tag_hits, "rows": body_rows})

    return render_template(
        "portal/semantics/_tags_panel.html",
        entities=entities,
        total_count=len(entities),
        all_tags=all_tags,
        tagged_property_count=int(catalog.get("tagged_property_count") or 0),
    )

def _version_history(
    *,
    is_admin: bool,
    versions_payload: dict[str, Any],
) -> str:
    versions = versions_payload.get("versions") or []
    active = versions_payload.get("active_version")
    pending_count = int(versions_payload.get("pending_count") or 0)
    rows = []
    for entry in versions:
        if not isinstance(entry, dict):
            continue
        ver = entry.get("version")
        is_active = active is not None and int(ver) == int(active)
        restore = (
            Markup(
                f'<button type="button" class="btn source-docs-restore-btn" '
                f'data-version="{escape(str(ver))}">Restore</button>'
            )
            if is_admin and not is_active
            else None
        )
        rows.append(
            {
                "version": str(ver),
                "is_active": is_active,
                "created": str(entry.get("created_at") or ""),
                "note": str(entry.get("note") or ""),
                "restore": restore,
            }
        )
    return render_template(
        "portal/semantics/_version_history.html", rows=rows, pending_count=pending_count
    )


def _workspace(
    payload: dict[str, Any],
    *,
    is_admin: bool,
) -> str:
    empty: set[str] = set()
    return render_template(
        "portal/semantics/_workspace.html",
        tables_panel=Markup(
            _tables_panel(payload.get("entity_properties"), is_admin=is_admin, pending_tables=empty)
        ),
        relationships_panel=Markup(
            _relationships_panel(
                payload.get("entity_relationships"), is_admin=is_admin, pending_relationships=empty
            )
        ),
        tags_panel=Markup(
            _tags_panel(payload.get("entity_property_tags"), is_admin=is_admin, pending_tags=empty)
        ),
    )


def render_source_docs_inspector_content_html(
    settings: DnaSettings,
    *,
    is_admin: bool,
    source: str | None = None,
) -> str:
    payload = load_source_docs_gold(settings, source=source)
    connector = str(payload.get("source") or settings.source)
    if not payload.get("available"):
        return _empty_state(
            is_admin=is_admin,
            source=connector,
            build_supported=bool(payload.get("build_supported")),
        )
    versions_payload = list_versions(settings, source=connector)
    # Pending removes are tracked client-side until Submit; do not SSR server overlay pending.
    versions_payload = {**versions_payload, "pending_count": 0, "pending": []}
    return (
        _summary_cards(
            payload.get("summary") or {},
            available=True,
            complete=bool(payload.get("complete")),
            silver_profile_present=bool(payload.get("silver_profile_present")),
            silver_reconciled=bool(payload.get("silver_reconciled")),
            silver_profile_key=str(payload.get("silver_profile_key") or ""),
        )
        + _workspace(payload, is_admin=is_admin)
        + _version_history(is_admin=is_admin, versions_payload=versions_payload)
    )



def _script(
    api_root: str,
    *,
    source: str,
    generated_at: str = "",
    artifact_generated_at: dict[str, str] | None = None,
) -> str:
    api = _json_for_script(api_root)
    source_js = _json_for_script(source)
    generated_js = _json_for_script(generated_at)
    artifact_generated_js = _json_for_script(artifact_generated_at or {})
    return f"""
<script>
(function () {{
  var apiRoot = {api};
  var activeSource = {source_js};
  var baselineGeneratedAt = {generated_js};
  var baselineArtifactGeneratedAt = {artifact_generated_js};
  var GOLD_ARTIFACTS = ["entity_properties", "entity_relationships", "entity_property_tags"];
  var statusEl = document.getElementById("source-docs-status");
  var pollTimer = null;
  var submitMode = false;
  var pendingQueue = [];

  function buildIsFresh(status) {{
    // Require all three gold catalogs (including tags) before treating rebuild/submit as done.
    if (!status.complete) return false;
    var times = ((status.summary || {{}}).artifact_generated_at) || {{}};
    var hasBaseline = GOLD_ARTIFACTS.some(function (key) {{
      return !!(baselineArtifactGeneratedAt && baselineArtifactGeneratedAt[key]);
    }});
    if (!hasBaseline) {{
      // First build: complete catalogs are enough.
      return true;
    }}
    // Rebuild/submit: every artifact must be rewritten (new generated_at), not just properties.
    return GOLD_ARTIFACTS.every(function (key) {{
      var next = times[key] || "";
      var prev = (baselineArtifactGeneratedAt && baselineArtifactGeneratedAt[key]) || "";
      return !!next && next !== prev;
    }});
  }}

  function storageKey() {{
    return "hiveflow:source-docs-pending:" + activeSource;
  }}

  function loadPending() {{
    try {{
      var raw = sessionStorage.getItem(storageKey());
      var parsed = raw ? JSON.parse(raw) : [];
      pendingQueue = Array.isArray(parsed) ? parsed : [];
    }} catch (err) {{
      pendingQueue = [];
    }}
  }}

  function savePending() {{
    try {{
      sessionStorage.setItem(storageKey(), JSON.stringify(pendingQueue));
    }} catch (err) {{}}
  }}

  function clearPendingStorage() {{
    pendingQueue = [];
    try {{
      sessionStorage.removeItem(storageKey());
    }} catch (err) {{}}
  }}

  function itemKey(item) {{
    var kind = item.kind || "";
    if (kind === "table") return "table|" + (item.table || "");
    if (kind === "relationship") {{
      return "relationship|" + (item.table || "") + "|" + (item.FK || "") + "|" + (item.target || "");
    }}
    if (kind === "tag") {{
      return "tag|" + (item.silver_entity || "") + "|" + (item.name || "") + "|" + (item.tag || "");
    }}
    return kind + "|" + JSON.stringify(item);
  }}

  function findPendingIndex(item) {{
    var key = itemKey(item);
    for (var i = 0; i < pendingQueue.length; i++) {{
      if (itemKey(pendingQueue[i]) === key) return i;
    }}
    return -1;
  }}

  function setStatus(message, isError) {{
    if (!statusEl) return;
    if (!message) {{
      statusEl.hidden = true;
      statusEl.textContent = "";
      statusEl.classList.remove("is-error");
      return;
    }}
    statusEl.hidden = false;
    statusEl.textContent = message;
    statusEl.classList.toggle("is-error", !!isError);
  }}

  function setPendingCount(count) {{
    var el = document.getElementById("source-docs-pending-count");
    if (el) el.textContent = String(count || 0);
    var btn = document.getElementById("source-docs-submit-btn");
    if (btn) btn.disabled = !(count > 0);
    var note = document.getElementById("source-docs-version-pending-note");
    if (note) {{
      if (count > 0) {{
        note.hidden = false;
        note.textContent = count + " pending change" + (count === 1 ? "" : "s") + " not yet submitted.";
      }} else {{
        note.hidden = true;
        note.textContent = "";
      }}
    }}
  }}

  function markButtonPending(btn, pending) {{
    if (!btn) return;
    btn.classList.toggle("is-pending", !!pending);
    btn.setAttribute("data-action", pending ? "undo" : "exclude");
    if (btn.classList.contains("source-docs-tag-remove")) {{
      btn.textContent = "×";
    }} else {{
      btn.textContent = pending ? "Undo" : "Remove";
    }}
  }}

  function targetForItem(item) {{
    var kind = item.kind;
    if (kind === "table") {{
      var table = item.table || "";
      var entities = [];
      document.querySelectorAll("#source-docs-tables-list .source-docs-entity").forEach(function (el) {{
        if ((el.getAttribute("data-table") || "") === table) entities.push(el);
      }});
      return entities;
    }}
    if (kind === "relationship") {{
      var rows = [];
      document.querySelectorAll("#source-docs-relationships-list tr").forEach(function (tr) {{
        var btn = tr.querySelector('.source-docs-edit-btn[data-kind="relationship"]');
        if (!btn) return;
        if (
          (btn.getAttribute("data-table") || "") === (item.table || "") &&
          (btn.getAttribute("data-fk") || "") === (item.FK || "") &&
          (btn.getAttribute("data-target") || "") === (item.target || "")
        ) {{
          rows.push(tr);
        }}
      }});
      return rows;
    }}
    if (kind === "tag") {{
      var chips = [];
      document.querySelectorAll("#source-docs-tags-list .source-docs-tag").forEach(function (chip) {{
        var btn = chip.querySelector('.source-docs-edit-btn[data-kind="tag"]');
        if (!btn) return;
        if (
          (btn.getAttribute("data-silver-entity") || "") === (item.silver_entity || "") &&
          (btn.getAttribute("data-name") || "") === (item.name || "") &&
          (btn.getAttribute("data-tag") || "") === (item.tag || "")
        ) {{
          chips.push(chip);
        }}
      }});
      return chips;
    }}
    return [];
  }}

  function applyItemVisual(item, pending) {{
    targetForItem(item).forEach(function (node) {{
      node.classList.toggle("is-pending-remove", !!pending);
      var btn = null;
      if (item.kind === "table") {{
        btn = node.querySelector('.source-docs-edit-btn[data-kind="table"]');
      }} else if (item.kind === "relationship") {{
        btn = node.querySelector('.source-docs-edit-btn[data-kind="relationship"]');
      }} else if (item.kind === "tag") {{
        btn = node.querySelector('.source-docs-edit-btn[data-kind="tag"]');
      }}
      markButtonPending(btn, pending);
    }});
  }}

  function refreshPendingVisuals() {{
    document.querySelectorAll(".is-pending-remove").forEach(function (el) {{
      el.classList.remove("is-pending-remove");
    }});
    document.querySelectorAll(".source-docs-edit-btn.is-pending").forEach(function (btn) {{
      markButtonPending(btn, false);
    }});
    pendingQueue.forEach(function (item) {{
      applyItemVisual(item, true);
    }});
    setPendingCount(pendingQueue.length);
  }}

  function activateTab(name) {{
    var section = document.getElementById("source-docs-tabs");
    if (!section) return;
    section.querySelectorAll("[data-source-docs-tab]").forEach(function (btn) {{
      var on = btn.getAttribute("data-source-docs-tab") === name;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    }});
    section.querySelectorAll("[data-source-docs-panel]").forEach(function (panel) {{
      panel.hidden = panel.getAttribute("data-source-docs-panel") !== name;
    }});
  }}

  function filterListByTable(selectId, listId) {{
    var select = document.getElementById(selectId);
    var list = document.getElementById(listId);
    if (!select || !list) return;
    var value = select.value || "";
    list.querySelectorAll(".source-docs-entity").forEach(function (el) {{
      var table = el.getAttribute("data-table") || "";
      el.hidden = !!(value && table !== value);
    }});
  }}

  function filterTables() {{
    filterListByTable("source-docs-table-filter", "source-docs-tables-list");
  }}

  function filterRelationships() {{
    filterListByTable("source-docs-rel-table-filter", "source-docs-relationships-list");
  }}

  function filterTags() {{
    var select = document.getElementById("source-docs-tag-table-filter");
    var input = document.getElementById("source-docs-tag-search");
    var list = document.getElementById("source-docs-tags-list");
    if (!list) return;
    var tableValue = select ? (select.value || "") : "";
    var query = input ? (input.value || "").trim().toLowerCase() : "";
    list.querySelectorAll(".source-docs-tag-table").forEach(function (section) {{
      var table = section.getAttribute("data-table") || "";
      if (tableValue && table !== tableValue) {{
        section.hidden = true;
        return;
      }}
      var rows = section.querySelectorAll(".source-docs-tag-row");
      var any = false;
      rows.forEach(function (row) {{
        var tags = (row.getAttribute("data-tags") || "");
        var match = !query || tags.indexOf(query) !== -1;
        row.hidden = !match;
        if (match) any = true;
        row.querySelectorAll(".source-docs-tag").forEach(function (chip) {{
          var tag = (chip.getAttribute("data-tag") || "");
          chip.classList.toggle("is-match", !!(query && tag.indexOf(query) !== -1));
        }});
      }});
      section.hidden = !any;
    }});
  }}

  function bindTabs() {{
    var section = document.getElementById("source-docs-tabs");
    if (!section) return;
    section.querySelectorAll("[data-source-docs-tab]").forEach(function (btn) {{
      btn.addEventListener("click", function () {{
        activateTab(btn.getAttribute("data-source-docs-tab") || "tables");
      }});
    }});
    activateTab(section.getAttribute("data-default-tab") || "tables");
  }}

  function bindFilters() {{
    var tableFilter = document.getElementById("source-docs-table-filter");
    if (tableFilter) tableFilter.addEventListener("change", filterTables);
    var relFilter = document.getElementById("source-docs-rel-table-filter");
    if (relFilter) relFilter.addEventListener("change", filterRelationships);
    var tagTableFilter = document.getElementById("source-docs-tag-table-filter");
    if (tagTableFilter) tagTableFilter.addEventListener("change", filterTags);
    var tagSearch = document.getElementById("source-docs-tag-search");
    if (tagSearch) {{
      tagSearch.addEventListener("input", filterTags);
      tagSearch.addEventListener("change", filterTags);
    }}
  }}

  function statusUrl() {{
    return apiRoot + "?source=" + encodeURIComponent(activeSource);
  }}

  async function fetchStatus() {{
    var response = await fetch(statusUrl(), {{ credentials: "same-origin" }});
    if (!response.ok) throw new Error("Status request failed (" + response.status + ")");
    return response.json();
  }}

  async function postJson(path, body) {{
    var response = await fetch(apiRoot + path, {{
      method: "POST",
      credentials: "same-origin",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify(body || {{}})
    }});
    var payload = await response.json().catch(function () {{ return {{}}; }});
    if (!response.ok) {{
      throw new Error(payload.error || payload.message || ("Request failed (" + response.status + ")"));
    }}
    return payload;
  }}

  async function startBuild(source) {{
    activeSource = source || activeSource;
    submitMode = false;
    setStatus("Building semantic model for " + activeSource + "…");
    ["source-docs-rebuild-btn", "source-docs-build-empty-btn", "source-docs-submit-btn"].forEach(function (id) {{
      var btn = document.getElementById(id);
      if (btn) btn.disabled = true;
    }});
    try {{
      var payload = await postJson("/build", {{
        source: activeSource,
        seed_missing_overlays: true,
        publish_schemas: false
      }});
      if (payload.status === "published") {{
        setStatus("Semantic model ready. Reloading…");
        window.location.reload();
        return;
      }}
      setStatus("Semantic model build queued. Waiting for catalogs…");
      startPolling();
    }} catch (err) {{
      setStatus(String(err && err.message ? err.message : err), true);
      ["source-docs-rebuild-btn", "source-docs-build-empty-btn"].forEach(function (id) {{
        var btn = document.getElementById(id);
        if (btn) btn.disabled = false;
      }});
      setPendingCount(pendingQueue.length);
    }}
  }}

  async function commitVersion() {{
    await postJson("/versions/commit", {{ source: activeSource, note: "Submitted" }});
    clearPendingStorage();
    setStatus("Changes committed. Reloading…");
    window.location.reload();
  }}

  async function startSubmit(source) {{
    activeSource = source || activeSource;
    loadPending();
    if (!pendingQueue.length) {{
      setStatus("No pending changes to submit.", true);
      return;
    }}
    submitMode = true;
    setStatus("Submitting " + pendingQueue.length + " overlay change(s) for " + activeSource + "…");
    ["source-docs-rebuild-btn", "source-docs-submit-btn"].forEach(function (id) {{
      var btn = document.getElementById(id);
      if (btn) btn.disabled = true;
    }});
    try {{
      var payload = await postJson("/submit", {{
        source: activeSource,
        excludes: pendingQueue
      }});
      if (payload.status === "published") {{
        clearPendingStorage();
        setStatus("Changes submitted and versioned. Reloading…");
        window.location.reload();
        return;
      }}
      setStatus("Gold merge queued. Waiting to commit version…");
      startPolling();
    }} catch (err) {{
      setStatus(String(err && err.message ? err.message : err), true);
      var rebuild = document.getElementById("source-docs-rebuild-btn");
      if (rebuild) rebuild.disabled = false;
      setPendingCount(pendingQueue.length);
      submitMode = false;
    }}
  }}

  function startPolling() {{
    if (pollTimer) clearInterval(pollTimer);
    var attempts = 0;
    pollTimer = setInterval(async function () {{
      attempts += 1;
      try {{
        var status = await fetchStatus();
        if (buildIsFresh(status)) {{
          clearInterval(pollTimer);
          if (submitMode) {{
            setStatus("Gold updated. Committing version…");
            try {{
              await commitVersion();
            }} catch (err) {{
              setStatus(String(err && err.message ? err.message : err), true);
            }}
            return;
          }}
          setStatus("Semantic model ready. Reloading…");
          window.location.reload();
          return;
        }}
        if (attempts >= 40) {{
          clearInterval(pollTimer);
          setStatus("Build is still running. Refresh this page in a minute.", true);
        }} else if (status.available && !status.complete) {{
          setStatus("Waiting for tags catalog to finish merging…");
        }} else if (baselineGeneratedAt || Object.keys(baselineArtifactGeneratedAt || {{}}).length) {{
          setStatus("Merging catalogs (tables, relationships, tags)…");
        }}
      }} catch (err) {{
        if (attempts >= 5) {{
          clearInterval(pollTimer);
          setStatus(String(err && err.message ? err.message : err), true);
        }}
      }}
    }}, 3000);
  }}

  function itemFromButton(btn) {{
    var kind = btn.getAttribute("data-kind") || "";
    if (kind === "table") {{
      return {{ kind: "table", table: btn.getAttribute("data-table") || "" }};
    }}
    if (kind === "relationship") {{
      return {{
        kind: "relationship",
        table: btn.getAttribute("data-table") || "",
        FK: btn.getAttribute("data-fk") || "",
        target: btn.getAttribute("data-target") || ""
      }};
    }}
    if (kind === "tag") {{
      var tag = btn.getAttribute("data-tag") || "";
      return {{
        kind: "tag",
        silver_entity: btn.getAttribute("data-silver-entity") || "",
        name: btn.getAttribute("data-name") || "",
        tag: tag,
        tags: [tag]
      }};
    }}
    return null;
  }}

  function handleExcludeClick(btn) {{
    var item = itemFromButton(btn);
    if (!item) return;
    var idx = findPendingIndex(item);
    if (idx >= 0) {{
      pendingQueue.splice(idx, 1);
      applyItemVisual(item, false);
      setStatus("Pending remove undone. Submit when ready.");
    }} else {{
      pendingQueue.push(item);
      applyItemVisual(item, true);
      setStatus("Marked for removal. Submit when ready.");
    }}
    savePending();
    setPendingCount(pendingQueue.length);
  }}

  async function handleRestore(btn) {{
    var version = btn.getAttribute("data-version");
    if (!version) return;
    if (!window.confirm("Restore version v" + version + "? This rewrites overlays and gold.")) {{
      return;
    }}
    btn.disabled = true;
    try {{
      await postJson("/restore", {{ source: activeSource, version: parseInt(version, 10) }});
      clearPendingStorage();
      setStatus("Restored version v" + version + ". Reloading…");
      window.location.reload();
    }} catch (err) {{
      btn.disabled = false;
      setStatus(String(err && err.message ? err.message : err), true);
    }}
  }}

  document.addEventListener("click", function (event) {{
    var target = event.target;
    if (!target) return;
    var editBtn = target.closest ? target.closest(".source-docs-edit-btn") : null;
    if (editBtn) {{
      handleExcludeClick(editBtn);
      return;
    }}
    var restoreBtn = target.closest ? target.closest(".source-docs-restore-btn") : null;
    if (restoreBtn) {{
      handleRestore(restoreBtn);
      return;
    }}
    if (!target.id) return;
    if (target.id === "source-docs-rebuild-btn" || target.id === "source-docs-build-empty-btn") {{
      startBuild(target.getAttribute("data-source") || activeSource);
    }} else if (target.id === "source-docs-submit-btn") {{
      startSubmit(target.getAttribute("data-source") || activeSource);
    }}
  }});

  loadPending();
  refreshPendingVisuals();
  bindTabs();
  bindFilters();
}})();
</script>
"""


def render_source_docs_inspector_page(
    request: Request,
    *,
    settings: DnaSettings,
    client: Any,
    is_admin: bool = False,
    html_response: Callable[..., Response],
    message: str = "",
    error: str = "",
    source: str | None = None,
    configured_sources: list[str] | None = None,
) -> Response:
    url = _url(request)
    sources = list_reference_sources(settings, configured=configured_sources)
    active = normalize_reference_source(source or "") or (sources[0] if sources else settings.source)
    if active not in sources:
        sources = [active, *[s for s in sources if s != active]]

    availability = {
        key: bool(load_source_docs_gold(settings, source=key).get("available")) for key in sources
    }
    payload = load_source_docs_gold(settings, source=active)
    available = bool(payload.get("available"))
    build_supported = bool(payload.get("build_supported", source_supports_gold_build(active)))
    api_root = url("/api/source-docs-gold")
    label = source_label(active)
    summary = payload.get("summary") or {}
    generated_at = str(summary.get("generated_at") or "")
    artifact_generated_at = summary.get("artifact_generated_at") or {}
    if not isinstance(artifact_generated_at, dict):
        artifact_generated_at = {}

    body = f"""
    <div class="source-docs-page semantic-builder-page" data-source="{escape(active)}">
      {page_header(
          "Source Browser",
          f"Inspect gold source catalogs for {label}. "
          "Reconciled gold uses live parquet column names for KPI SQL.",
      )}
      {_source_switcher(
          sources=sources,
          active_source=active,
          url=url,
          availability=availability,
      )}
      {_admin_nav(
          available=available,
          is_admin=is_admin,
          build_supported=build_supported,
          source=active,
          pending_count=0,
      )}
    """
    if message:
        body += f'<div class="form-success">{escape(message)}</div>'
    if error:
        body += f'<div class="form-error">{escape(error)}</div>'
    body += """
      <div id="source-docs-status" class="source-docs-status" hidden></div>
      <div id="source-docs-content">
    """
    body += render_source_docs_inspector_content_html(
        settings,
        is_admin=is_admin,
        source=active,
    )
    body += "</div>"
    body += f'<link rel="stylesheet" href="{escape(url("/static/source-docs-inspector.css"))}" />'
    body += _script(
        api_root,
        source=active,
        generated_at=generated_at,
        artifact_generated_at={
            str(k): str(v or "") for k, v in artifact_generated_at.items() if str(k).strip()
        },
    )
    body += "</div>"

    return html_response(
        request,
        client=client,
        title=f"Source Browser — {label}",
        active_path=SOURCE_DOCS_INSPECTOR_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )
