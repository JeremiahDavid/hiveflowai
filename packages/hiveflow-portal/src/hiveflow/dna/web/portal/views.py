"""Protected client portal reporting views."""

from __future__ import annotations

from datetime import datetime
from hiveflow.compat import UTC
from typing import Any, Callable

from markupsafe import Markup
from werkzeug.wrappers import Request, Response

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import load_pack_from_settings, read_json_artifact, read_production_output
from hiveflow.dna.web.portal.config import ClientPortalConfig
from hiveflow.dna.web.charts import charts_page_assets
from hiveflow.dna.web.charts.catalog import CHART_TYPE_CATALOG
from hiveflow.dna.web.charts.demo import chart_demo_has_charts, chart_demo_section_html
from hiveflow.dna.web.charts.gold import (
    REVENUE_OUTPUT_ID,
    aggregate_revenue_by_month,
)
from hiveflow.dna.web.templating import render_template
from hiveflow.dna.web.theme import (
    TAGLINE,
    badge_row,
    empty_state,
    escape,
    page_header,
    render_portal_page,
)
from hiveflow.dna.web.portal.catalog import CATALOG_ROOT
from hiveflow.dna.web.portal.dna_nav import (
    DNA_ROOT,
    KPI_GENERATOR_ROOT,
    SOURCE_DOCS_INSPECTOR_ROOT,
    agents_section_nav,
    dna_engine_site_url,
    dna_section_nav,
)
from hiveflow.dna.web.routing_helpers import _app_url
from hiveflow.dna.web.portal.reporting_layout import (
    is_chart_catalog_page,
    reporting_data_menu,
    reporting_quick_links,
)
from hiveflow.dna.web.portal.reporting_render import (
    DEFAULT_CHART_MONTHS,
    DEFAULT_TABLE_LIMIT,
    generic_table_html,
    page_eyebrow,
    page_has_content,
    render_page_body,
)
from hiveflow.dna.workflow import load_workflow_state

REVENUE_OUTPUT_ID = REVENUE_OUTPUT_ID
REVENUE_TABLE_LIMIT = DEFAULT_TABLE_LIMIT
REVENUE_TREND_MONTHS = DEFAULT_CHART_MONTHS

AGENTS_ROOT = KPI_GENERATOR_ROOT

# DNA/Agents/Governance now live entirely on the DNA Engine subdomain (see
# docs/dna-engine.md) — the shell no longer serves any of these paths itself,
# so these top-bar tabs are absolute cross-subdomain links built at request
# time (dna_engine_site_url() reads HIVEFLOW_PORTAL_COOKIE_DOMAIN, not fixed
# at import time). Falls back to the bare relative path when no cookie
# domain is configured (local dev without multi-tenant DNS wiring).
def _portal_nav() -> tuple[tuple[str, str], ...]:
    return (
        (dna_engine_site_url(DNA_ROOT), "DNA"),
        (dna_engine_site_url(AGENTS_ROOT), "Agents"),
        (dna_engine_site_url("/governance"), "Governance"),
    )


# In-page sub-nav under Governance (always visible; pages enforce admin
# auth). NOTE: this module's DNA/Agents/Governance render_* functions
# (render_catalog, render_governance, etc.) are reused unchanged by DNA
# Engine (see hiveflow.dna_engine.web.routes) — this constant, and
# _portal_side_nav below, are still exercised there even though the shell
# itself no longer serves any page whose active_path would match them.
GOVERNANCE_SECTION_NAV = (
    ("/governance", "Pack Registry"),
    ("/governance/users", "Users"),
)



# Legacy fallbacks when reporting config cannot be loaded (tests / empty seed).
PORTAL_DATA_MENU = (
    ("/portal", "Summary"),
    ("/portal/executive", "Executive"),
    (
        "/portal/sales",
        "Sales",
        (
            ("/portal/revenue", "Order-to-cash detail"),
            ("/portal/revenue-trend", "Revenue trend"),
        ),
    ),
    ("/portal/operations", "Operations"),
    ("/portal/finance", "Finance"),
    ("/portal/inventory", "Inventory"),
)

PORTAL_REPORT_PAGES = (
    ("/portal/executive", "Executive KPIs", "Full metric cards with definitions and pack provenance"),
    ("/portal/revenue", "Order-to-cash detail", "Posted invoice lines from certified gold output"),
    ("/portal/revenue-trend", "Revenue trend", "Monthly posted revenue from certified invoice lines"),
)

def _format_published_date(published_at: Any) -> str:
    text = str(published_at).strip()
    if not text:
        return text
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%b %d, %Y")
    except ValueError:
        return text[:10] if len(text) >= 10 else text


def _format_datetime_minute(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        has_time = (
            "T" in text
            or text.endswith("Z")
            or "+" in text[10:]
            or (len(text) > 10 and text[10] in {" ", "T"})
        )
        if has_time:
            return dt.strftime("%b %d, %Y %H:%M")
        return dt.strftime("%b %d, %Y")
    except ValueError:
        if len(text) >= 10:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%b %d, %Y")
            except ValueError:
                pass
        return text






def _history_entry_target(entry: dict[str, Any]) -> str:
    target = str(entry.get("target") or "").strip().lower()
    if target:
        return target
    notes = str(entry.get("notes") or "")
    if "Updated via client portal" in notes:
        return "all"
    return "dna"


def _filter_pack_history(history: list[Any], pack_kind: str) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        target = _history_entry_target(entry)
        if pack_kind == "dna" and target in {"dna", "all"}:
            filtered.append(entry)
        elif pack_kind == "reporting" and target in {"reporting", "all"}:
            filtered.append(entry)
    return filtered


def _history_approval_for_version(
    history: list[dict[str, Any]],
    version: str,
) -> tuple[str, str]:
    target_version = str(version or "").strip()
    if not target_version:
        return "—", "—"
    for entry in reversed(history):
        if str(entry.get("version") or "").strip() != target_version:
            continue
        approver = str(entry.get("approver") or "").strip() or "—"
        at = str(entry.get("at") or "").strip()
        approved = _format_datetime_minute(at) if at else "—"
        return approver, approved
    return "—", "—"


def _reporting_pack_counts(reporting: dict[str, Any]) -> tuple[int, int]:
    pages = reporting.get("pages") or []
    page_count = sum(1 for page in pages if isinstance(page, dict))
    visualization_count = 0
    for page in pages:
        if not isinstance(page, dict):
            continue
        visualization_count += sum(
            1 for chart in (page.get("charts") or []) if isinstance(chart, dict)
        )
        for section in page.get("sections") or []:
            if not isinstance(section, dict):
                continue
            if section.get("layout") == "chart" or isinstance(section.get("chart"), dict):
                visualization_count += 1
    return page_count, visualization_count


def _history_table_rows(
    history: list[dict[str, Any]],
    *,
    pack_kind: str = "dna",
    active_version: str = "",
    is_admin: bool = False,
    form_action: str = "",
    settings: DnaSettings | None = None,
) -> str:
    from hiveflow.dna.web.portal.governance_restore import (
        RestoreTarget,
        governance_target_snapshot_exists,
    )

    target: RestoreTarget = "dna" if pack_kind == "dna" else "reporting"
    restore_action = "restore_dna" if target == "dna" else "restore_reporting"
    label = "DNA" if target == "dna" else "reporting"
    col_count = 6 if is_admin else 5
    rows = []
    for entry in reversed(history):
        version = str(entry.get("version") or "").strip()
        action: dict[str, Any] = {"kind": "none"}
        if is_admin:
            if version and version == str(active_version or "").strip():
                action = {"kind": "current"}
            elif (
                version
                and form_action
                and settings is not None
                and governance_target_snapshot_exists(
                    settings, target=target, version=version
                )
            ):
                confirm = (
                    f"Restore {label} from v{version}? "
                    "This creates a new patch version and pins it as production. "
                    "Gold outputs are not republished automatically."
                )
                action = {
                    "kind": "restore",
                    "form_action": form_action,
                    "confirm_repr": repr(confirm),
                    "restore_action": restore_action,
                    "version": version,
                }
            else:
                action = {"kind": "dash"}
        rows.append(
            {
                "version": version or "—",
                "status": str(entry.get("status", "—")),
                "approver": entry.get("approver") or "—",
                "published": _format_published_date(entry.get("at")) if entry.get("at") else "—",
                "notes": entry.get("notes") or "—",
                "action": action,
            }
        )
    return render_template(
        "portal/_history_table_rows.html", rows=rows, is_admin=is_admin, col_count=col_count
    )


def _portal_nav_links(*, is_admin: bool = False) -> tuple[tuple[str, str], ...]:
    del is_admin  # top nav is the same; admin tools live under Governance sub-nav
    return _portal_nav()


def _portal_nav_active_path(active_path: str) -> str:
    # _portal_nav()'s DNA/Agents/Governance hrefs are absolute DNA Engine
    # URLs now — the top-nav template compares this return value against
    # those raw hrefs for string equality (theme.py::_nav_items), so a match
    # here must be wrapped the same way, whichever app is currently
    # rendering (the shell, remapping one of its own Reporting pages that
    # will never actually match these prefixes; or DNA Engine itself,
    # reusing this exact function for its own top-nav highlighting).
    if active_path.startswith(AGENTS_ROOT):
        return dna_engine_site_url(AGENTS_ROOT)
    if (
        active_path.startswith(SOURCE_DOCS_INSPECTOR_ROOT)
        or active_path.startswith(CATALOG_ROOT)
        or active_path.startswith(DNA_ROOT)
    ):
        return dna_engine_site_url(DNA_ROOT)
    if active_path.startswith("/governance"):
        return dna_engine_site_url("/governance")
    return active_path


def _preview_banner_html(
    *,
    next_version: str,
    proposal_id: str,
) -> str:
    return render_template(
        "portal/_preview_banner.html",
        next_version=next_version,
        proposal_id=proposal_id,
        back_url=dna_engine_site_url("/governance"),
        kpi_url=dna_engine_site_url(KPI_GENERATOR_ROOT),
        exit_url=dna_engine_site_url("/governance/config/preview/exit"),
    )


def _report_page_links_html(
    url: Callable[[str], str],
    *,
    settings: DnaSettings | None = None,
    reporting_override: dict[str, Any] | None = None,
) -> str:
    if settings is not None:
        pages = reporting_quick_links(settings, override=reporting_override)
    else:
        pages = PORTAL_REPORT_PAGES
    links = [
        {"href": url(path), "title": title, "subtitle": subtitle} for path, title, subtitle in pages
    ]
    return render_template("portal/_quick_links.html", links=links)


_PILLAR_LABELS = {
    "executive": "Executive",
    "sales": "Sales",
    "operations": "Operations",
    "finance": "Finance",
    "inventory": "Inventory",
    "developer": "Developer",
}


def _pillar_grouped_links_html(
    url: Callable[[str], str],
    *,
    settings: DnaSettings | None = None,
    reporting_override: dict[str, Any] | None = None,
) -> str:
    from hiveflow.dna.web.portal.reporting_layout import list_reporting_pages

    if settings is None:
        return _report_page_links_html(url, settings=settings, reporting_override=reporting_override)

    by_pillar: dict[str, list[dict[str, Any]]] = {}
    for page in list_reporting_pages(settings, override=reporting_override):
        path = page.get("path") or ""
        if path in {"/portal", "/portal/"}:
            continue
        pillar = str(page.get("pillar") or "sales")
        by_pillar.setdefault(pillar, []).append(page)

    order = ("executive", "sales", "operations", "finance", "inventory", "developer")
    sections = []
    for pillar in order:
        pages = by_pillar.get(pillar) or []
        if not pages:
            continue
        content_pages = [
            page
            for page in pages
            if page_has_content(page) or page.get("chart_catalog")
        ]
        # When a pillar has detail pages, omit the empty hub landing from quick links.
        pages = content_pages or pages
        label = _PILLAR_LABELS.get(pillar, pillar.title())
        links = [
            {
                "href": url(page["path"]),
                "title": page["title"],
                "subtitle": page.get("description") or "",
            }
            for page in pages
        ]
        sections.append({"label": label, "links": links})
    if not sections:
        return _report_page_links_html(url, settings=settings, reporting_override=reporting_override)
    return render_template("portal/_pillar_grouped_links.html", sections=sections)


def _pillar_hub_links(page: dict[str, Any], url: Callable[[str], str], *, settings: DnaSettings) -> str:
    from hiveflow.dna.web.portal.reporting_layout import list_reporting_pages

    pillar = str(page.get("pillar") or "")
    related = [
        item
        for item in list_reporting_pages(settings)
        if str(item.get("pillar") or "") == pillar
        and item.get("path") not in {"/portal", page.get("path")}
        and (item.get("tables") or item.get("charts") or item.get("sections"))
    ]
    if not related:
        return empty_state(
            "Reports coming soon",
            "Additional pages for this pillar will appear here as they are configured.",
        )
    links = [
        {
            "href": url(item["path"]),
            "title": item["title"],
            "subtitle": item.get("description") or "",
        }
        for item in related
    ]
    return render_template("portal/_quick_links.html", links=links)


def _portal_side_nav(
    active_path: str,
    data_menu: tuple[Any, ...],
    dna_menu: tuple[Any, ...] | None = None,
    agents_menu: tuple[Any, ...] | None = None,
) -> tuple[str | None, tuple[Any, ...] | None, str | None]:
    # active_path here is always the RAW (relative) path passed to
    # _html_response by whichever app called it — the shell's own remaining
    # routes only ever pass "/portal/..." Reporting Engine paths (last
    # branch below); the DNA/Agents/Governance branches only ever match when
    # DNA Engine reuses this function for its own pages (see views.py's
    # module docstring note on GOVERNANCE_SECTION_NAV above).
    if active_path.startswith(AGENTS_ROOT):
        return "Agents", agents_menu or agents_section_nav(), "agents"
    if (
        active_path.startswith(SOURCE_DOCS_INSPECTOR_ROOT)
        or active_path.startswith(CATALOG_ROOT)
        or active_path.startswith(DNA_ROOT)
    ):
        return "DNA", dna_menu or dna_section_nav(None), "dna"
    if active_path.startswith("/governance"):
        return "Governance", GOVERNANCE_SECTION_NAV, "governance"
    if active_path.startswith("/portal"):
        return "Reporting", data_menu, "reporting"
    return None, None, None


def _html_response(
    request: Request,
    *,
    client: ClientPortalConfig,
    title: str,
    active_path: str,
    body: str,
    page_title: str | None = None,
    use_charts: bool = False,
    is_admin: bool = False,
    settings: DnaSettings | None = None,
    reporting_override: dict[str, Any] | None = None,
    preview_meta: dict[str, Any] | None = None,
) -> Response:
    url = lambda path: _app_url(request, path)
    charts_assets = charts_page_assets(url) if use_charts else ""
    data_menu = (
        reporting_data_menu(settings, override=reporting_override)
        if settings is not None
        else PORTAL_DATA_MENU
    )
    if not data_menu:
        data_menu = PORTAL_DATA_MENU
    dna_menu = dna_section_nav(settings)
    agents_menu = agents_section_nav()
    if preview_meta:
        body = (
            _preview_banner_html(
                next_version=str(preview_meta.get("next_version") or ""),
                proposal_id=str(preview_meta.get("proposal_id") or ""),
            )
            + body
        )
    sidebar_active_path = active_path
    nav_active_path = _portal_nav_active_path(active_path)
    side_nav_title, side_nav_items, side_nav_id = _portal_side_nav(
        active_path, data_menu, dna_menu, agents_menu
    )
    return Response(
        render_portal_page(
            title=title,
            active_path=nav_active_path,
            sidebar_active_path=sidebar_active_path,
            body=body,
            page_title=page_title,
            client=client,
            nav_links=_portal_nav_links(is_admin=is_admin),
            data_menu=data_menu,
            side_nav_title=side_nav_title,
            side_nav_items=side_nav_items,
            side_nav_id=side_nav_id,
            url=url,
            charts_assets=charts_assets,
        ),
        mimetype="text/html",
    )


def render_overview(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    page: dict[str, Any] | None = None,
    reporting_override: dict[str, Any] | None = None,
    preview_meta: dict[str, Any] | None = None,
) -> Response:
    page = page or {}
    workflow = load_workflow_state(settings, settings.dna_config_id)
    manifest = read_json_artifact(settings, f"{settings.gold_dna_prefix}/manifest.json") or {}
    url: Callable[[str], str] = lambda path: _app_url(request, path)
    active_path = str(page.get("path") or "/portal")
    title = str(page.get("title") or "Reporting")

    badges: list[tuple[str, bool]] = []
    active = workflow.get("active_version")
    if active:
        badges.append((f"v{active} production", True))
    if manifest.get("published_at"):
        badges.append((f"Updated {_format_published_date(manifest.get('published_at'))}", True))

    body = page_header(client.welcome_title, client.welcome_message, eyebrow=TAGLINE)
    if badges:
        body += badge_row(*badges)
    if page_has_content(page):
        sections_html, _use_charts = render_page_body(page, settings=settings)
        body += sections_html
    body += f"""
    <section class="section">
      <div class="section-title">Reports by pillar</div>
      <div class="card">
        {_pillar_grouped_links_html(url, settings=settings, reporting_override=reporting_override)}
      </div>
    </section>
    """
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=active_path,
        body=body,
        is_admin=is_admin,
        settings=settings,
        reporting_override=reporting_override,
        preview_meta=preview_meta,
    )


def render_generic_page(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    page: dict[str, Any] | None = None,
    reporting_override: dict[str, Any] | None = None,
    preview_meta: dict[str, Any] | None = None,
) -> Response:
    page = page or {}
    title = str(page.get("title") or "Report")
    description = str(page.get("description") or "Configured in the company reporting pack.")
    active_path = str(page.get("path") or "/portal")
    body = page_header(title, description, eyebrow=page_eyebrow(page))
    page_html, use_charts = render_page_body(page, settings=settings)
    body += page_html
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=active_path,
        body=body,
        use_charts=use_charts,
        is_admin=is_admin,
        settings=settings,
        reporting_override=reporting_override,
        preview_meta=preview_meta,
    )


def render_pillar_hub(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    page: dict[str, Any] | None = None,
    reporting_override: dict[str, Any] | None = None,
    preview_meta: dict[str, Any] | None = None,
) -> Response:
    page = page or {}
    title = str(page.get("title") or "Reports")
    description = str(page.get("description") or "Configured reports for this business pillar.")
    active_path = str(page.get("path") or "/portal")
    pillar = str(page.get("pillar") or "sales").title()
    url: Callable[[str], str] = lambda path: _app_url(request, path)
    body = page_header(title, description, eyebrow=pillar)
    body += f'<section class="section"><div class="card">{_pillar_hub_links(page, url, settings=settings)}</div></section>'
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=active_path,
        body=body,
        is_admin=is_admin,
        settings=settings,
        reporting_override=reporting_override,
        preview_meta=preview_meta,
    )


def render_chart_demo(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    page: dict[str, Any] | None = None,
    reporting_override: dict[str, Any] | None = None,
    preview_meta: dict[str, Any] | None = None,
) -> Response:
    title = str((page or {}).get("title") or "Chart catalog")
    description = str(
        (page or {}).get("description")
        or f"Interactive gallery of reporting chart types sourced from certified gold outputs ({REVENUE_OUTPUT_ID}, out_dim_items)."
    )
    active_path = str((page or {}).get("path") or "/portal/chart-demo")
    body = page_header(title, description, eyebrow="ECharts")
    body += badge_row((f"{len(CHART_TYPE_CATALOG)} chart types", True), ("Gold-backed demo", False))
    body += f"""
    <section class="section">
      <div class="section-title">Catalog</div>
      {chart_demo_section_html(settings)}
    </section>
    """
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=active_path,
        body=body,
        use_charts=chart_demo_has_charts(settings),
        is_admin=is_admin,
        settings=settings,
        reporting_override=reporting_override,
        preview_meta=preview_meta,
    )


def render_dna(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
) -> Response:
    """DNA section landing — open the first catalog table when available."""
    return render_catalog(
        request,
        settings=settings,
        client=client,
        is_admin=is_admin,
    )


def render_catalog(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
) -> Response:
    """Catalog landing — open the first gold table, or an empty state."""
    from hiveflow.dna.web.portal.catalog import CATALOG_ROOT, list_catalog_tables

    tables = list_catalog_tables(settings)
    if tables:
        target = f"{CATALOG_ROOT}/{tables[0].id}"
        return Response(
            status=302,
            headers={
                "Location": f"{request.script_root}{target}",
            },
        )
    body = page_header(
        "DNA Catalog",
        "Certified gold tables appear here after DNA publish completes.",
        eyebrow="DNA",
    )
    body += empty_state(
        "No gold tables yet",
        "Publish a DNA pack with table outputs to browse them here.",
    )
    return _html_response(
        request,
        client=client,
        title="DNA Catalog",
        active_path=CATALOG_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_catalog_table(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    output_id: str,
    is_admin: bool = False,
) -> Response:
    """Preview a single gold table (all columns, limited rows)."""
    from hiveflow.dna.web.portal.catalog import (
        CATALOG_PREVIEW_LIMIT,
        CATALOG_ROOT,
        catalog_table_config,
        catalog_table_label,
        find_catalog_table,
    )

    output = find_catalog_table(settings, output_id)
    if output is None:
        return Response("Not found", status=404, mimetype="text/plain")

    title = catalog_table_label(output)
    active_path = f"{CATALOG_ROOT}/{output.id}"
    body = page_header(
        title,
        f"Gold preview · first {CATALOG_PREVIEW_LIMIT} rows · all columns",
        eyebrow="DNA · Catalog",
    )
    body += f"""
    <section class="section">
      {generic_table_html(
          settings,
          catalog_table_config(output),
          empty_title="No rows yet",
          empty_detail="This gold table has not been published yet.",
      )}
    </section>
    """
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=active_path,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_data_profile_index(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    configured_sources: list[str] | None = None,
) -> Response:
    from hiveflow.dna.web.portal.data_profile_ui.render import render_data_profile_index_page
    from hiveflow.dna.web.portal.data_profile_ui.service import list_profile_rows
    from hiveflow.dna.web.portal.dna_nav import DATA_PROFILE_ROOT

    url: Callable[[str], str] = lambda path: _app_url(request, path)
    rows = list_profile_rows(settings, configured_sources=configured_sources)
    body = page_header(
        "Data Profile",
        "Per-table, per-field profiling stats and Bedrock-generated descriptions — review, re-run, or override.",
        eyebrow="DNA",
    )
    body += render_data_profile_index_page(url=url, rows=rows)
    return _html_response(
        request,
        client=client,
        title="Data Profile",
        active_path=DATA_PROFILE_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_data_profile_detail(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    source: str,
    entity: str,
    is_admin: bool = False,
    message: str = "",
    error: str = "",
) -> Response:
    from hiveflow.dna.web.portal.data_profile_ui.render import render_data_profile_detail_page
    from hiveflow.dna.web.portal.data_profile_ui.service import load_profile
    from hiveflow.dna.web.portal.dna_nav import DATA_PROFILE_ROOT

    url: Callable[[str], str] = lambda path: _app_url(request, path)
    profile = load_profile(settings, source, entity)
    body = page_header(f"{source}.{entity}", "Data profile", eyebrow="DNA · Data Profile")
    if message:
        body += empty_state(message, "")
    if error:
        body += empty_state("Error", error)
    body += render_data_profile_detail_page(url=url, source=source, entity=entity, profile=profile)
    return _html_response(
        request,
        client=client,
        title=f"{source}.{entity}",
        active_path=DATA_PROFILE_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_model_mapping(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    entity: str = "",
    promote_report: dict[str, Any] | None = None,
    message: str = "",
    error: str = "",
) -> Response:
    from hiveflow.dna.web.portal.dna_nav import MODEL_MAPPING_ROOT
    from hiveflow.dna.web.portal.model_mapping.render import render_model_mapping_page
    from hiveflow.dna.web.portal.model_mapping.service import (
        available_templates,
        load_current_mapping,
        load_template_for,
    )
    from hiveflow.dna.industry_mapping import mapping_completion

    url: Callable[[str], str] = lambda path: _app_url(request, path)
    mapping = load_current_mapping(settings)
    template = load_template_for(mapping) if mapping is not None else None
    completion = mapping_completion(mapping, template) if mapping is not None else None

    body = page_header(
        "Model Mapping",
        "Bind this client's profiled data onto an industry data model, approve field mappings, and promote to a governed DefinitionPack.",
        eyebrow="DNA",
    )
    if message:
        body += empty_state(message, "")
    if error:
        body += empty_state("Error", error)
    body += render_model_mapping_page(
        url=url,
        mapping=mapping,
        template=template,
        completion=completion,
        selected_entity=entity,
        promote_report=promote_report,
        available=available_templates(),
    )
    return _html_response(
        request,
        client=client,
        title="Model Mapping",
        active_path=MODEL_MAPPING_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_catalog_gold(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
) -> Response:
    from hiveflow.dna.web.portal.catalog import GOLD_CATALOG_ROOT, list_catalog_tables

    tables = list_catalog_tables(settings)
    if tables:
        target = f"{CATALOG_ROOT}/{tables[0].id}"
        return Response(
            status=302,
            headers={"Location": f"{request.script_root}{target}"},
        )
    body = page_header(
        "Gold",
        "Certified gold tables appear here after DNA publish completes.",
        eyebrow="DNA · Catalog",
    )
    body += empty_state(
        "No gold tables yet",
        "Publish a DNA pack with table outputs to browse them here.",
    )
    return _html_response(
        request,
        client=client,
        title="Gold",
        active_path=GOLD_CATALOG_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_catalog_silver(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    entity: str = "",
    is_admin: bool = False,
) -> Response:
    from hiveflow.dna.field_semantics import list_silver_entities
    from hiveflow.dna.web.portal.catalog import (
        SILVER_CATALOG_ROOT,
        find_silver_entity,
        silver_entity_label,
        silver_preview_table_html,
    )
    from hiveflow.dna.web.portal.dna_nav import source_label

    entities = list_silver_entities(settings)
    entity_name = entity.strip().lower()
    if not entity_name and entities:
        target = f"{SILVER_CATALOG_ROOT}/{entities[0]}"
        return Response(
            status=302,
            headers={"Location": f"{request.script_root}{target}"},
        )

    if not entities:
        body = page_header(
            "Silver",
            "Ingested connector entities appear here after silver consolidation.",
            eyebrow="DNA · Catalog",
        )
        body += empty_state(
            "No silver entities configured",
            "Connect and ingest a data source to browse silver tables here.",
        )
        return _html_response(
            request,
            client=client,
            title="Silver",
            active_path=SILVER_CATALOG_ROOT,
            body=body,
            is_admin=is_admin,
            settings=settings,
        )

    if entity_name and find_silver_entity(settings, entity_name) is None:
        return Response("Not found", status=404, mimetype="text/plain")

    source = settings.source.strip().lower()
    title = silver_entity_label(entity_name)
    active_path = f"{SILVER_CATALOG_ROOT}/{entity_name}"
    body = page_header(
        title,
        f"Silver preview · {source_label(source)}",
        eyebrow="DNA · Catalog",
    )
    body += f"""
    <section class="section">
      {silver_preview_table_html(settings, entity_name)}
    </section>
    """
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=active_path,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )




def render_configured_page(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    page: dict[str, Any],
    is_admin: bool = False,
    reporting_override: dict[str, Any] | None = None,
    preview_meta: dict[str, Any] | None = None,
) -> Response:
    """Dispatch a reporting-config page to the matching renderer."""
    page_id = str(page.get("id") or "")
    path = str(page.get("path") or "")
    common = dict(
        settings=settings,
        client=client,
        is_admin=is_admin,
        page=page,
        reporting_override=reporting_override,
        preview_meta=preview_meta,
    )

    if page_id == "page_summary" or path in {"/portal", "/portal/"}:
        return render_overview(request, **common)
    if is_chart_catalog_page(page):
        return render_chart_demo(request, **common)
    if page_id in {"page_sales", "page_operations", "page_finance", "page_inventory"} and not page_has_content(
        page
    ):
        return render_pillar_hub(request, **common)
    if page_has_content(page):
        return render_generic_page(request, **common)
    # Unknown page shape — still show title/description from config.
    title = str(page.get("title") or "Report")
    description = str(page.get("description") or "Configured in the company reporting pack.")
    body = page_header(title, description, eyebrow="Report")
    body += empty_state(
        "No charts or tables configured",
        "Add charts or tables with source_output bindings in the reporting config.",
    )
    return _html_response(
        request,
        client=client,
        title=title,
        active_path=path or "/portal",
        body=body,
        is_admin=is_admin,
        settings=settings,
        reporting_override=reporting_override,
        preview_meta=preview_meta,
    )


def _pack_to_yaml(payload: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)








from hiveflow.dna.web.portal.version_bump import (
    version_bump_field_html as _version_bump_field_html,
    version_bump_script as _version_bump_script,
)




















def _append_portal_governance_history(
    state: dict[str, Any],
    *,
    version: str,
    status: str,
    approver: str,
    target: str,
    notes: str,
) -> None:
    history = state.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "version": version,
            "status": status,
            "approver": approver or "Portal admin",
            "at": datetime.now(UTC).isoformat(),
            "notes": notes,
            "target": target,
        }
    )
    state["history"] = history


def save_governance_dna_from_portal(
    settings: DnaSettings,
    *,
    dna_yaml: str,
    dna_version: str,
    pin_production: bool,
    approver: str,
) -> dict[str, Any]:
    """Parse portal DNA YAML and persist a governance version."""
    from hiveflow.dna.governance import save_governance_version
    from hiveflow.dna.schema import load_definition_pack_yaml
    from hiveflow.dna.web.portal.governance_helpers.proposals import (
        bump_major_version,
        bump_minor_version,
        bump_patch_version,
        classify_manual_version_bump,
    )
    from hiveflow.dna.workflow import load_workflow_state, save_workflow_state

    dna_version = dna_version.strip()
    if not dna_version:
        raise ValueError("DNA version is required")

    state = load_workflow_state(settings, settings.dna_config_id)
    pack = load_definition_pack_yaml(dna_yaml)
    pack.pack_id = settings.dna_config_id
    dna_base = str(state.get("active_version") or pack.version or "")

    dna_bump = classify_manual_version_bump(dna_base, dna_version)
    if dna_bump["kind"] == "invalid":
        raise ValueError(f"DNA: {dna_bump['error']}")

    pack.version = dna_version
    if pin_production:
        pack.status = "production"
        pack.approval.status = "production"
        pack.approval.approver = approver or pack.approval.approver or "Portal admin"
        pack.approval.approved_at = datetime.now(UTC).date().isoformat()
    else:
        pack.status = "draft"
        pack.approval.status = "draft"

    notes = "Updated DNA via client portal manual edit"
    if dna_bump["kind"] in {"minor", "major"} and dna_bump.get("warning"):
        notes = f"{notes}. {dna_bump['warning']}"

    saved = save_governance_version(settings, pack=pack, reporting=None)
    state["pack_id"] = settings.dna_config_id
    _append_portal_governance_history(
        state,
        version=dna_version,
        status=pack.approval.status,
        approver=approver,
        target="dna",
        notes=notes,
    )
    if pin_production:
        prior_dna_pin = str(state.get("active_version") or dna_base)
        if not state.get("active_reporting_version"):
            state["active_reporting_version"] = prior_dna_pin
        state["active_version"] = dna_version
    save_workflow_state(settings, state)
    return {
        "status": "saved",
        "target": "dna",
        "dna_version": dna_version,
        "version": dna_version,
        "approval_status": pack.approval.status,
        "bump_kind": dna_bump["kind"],
        "warning": dna_bump.get("warning") or "",
        "dna_base_version": dna_base,
        "dna_next_patch": bump_patch_version(dna_base),
        "dna_next_minor": bump_minor_version(dna_base),
        "dna_next_major": bump_major_version(dna_base),
        **{k: saved[k] for k in ("dna_path", "manifest_path") if k in saved},
    }


def save_governance_reporting_from_portal(
    settings: DnaSettings,
    *,
    reporting_yaml: str,
    reporting_version: str,
    pin_production: bool,
    approver: str,
) -> dict[str, Any]:
    """Parse portal reporting YAML and persist a governance version."""
    from hiveflow.dna.web.portal.governance_helpers.proposals import (
        bump_major_version,
        bump_minor_version,
        bump_patch_version,
        classify_manual_version_bump,
    )
    from hiveflow.dna.reporting import (
        load_reporting_pack_yaml,
        normalize_reporting_identity,
        save_reporting_pack,
    )
    from hiveflow.dna.workflow import load_workflow_state, save_workflow_state

    reporting_version = reporting_version.strip()
    if not reporting_version:
        raise ValueError("Reporting version is required")

    state = load_workflow_state(settings, settings.dna_config_id)
    reporting_base = str(
        state.get("active_reporting_version") or state.get("active_version") or ""
    )

    reporting_bump = classify_manual_version_bump(reporting_base, reporting_version)
    if reporting_bump["kind"] == "invalid":
        raise ValueError(f"Reporting: {reporting_bump['error']}")

    reporting_status = "production" if pin_production else "draft"
    reporting = load_reporting_pack_yaml(reporting_yaml)
    reporting = normalize_reporting_identity(
        settings,
        reporting,
        version=reporting_version,
        status=reporting_status,
    )

    notes = "Updated reporting via client portal manual edit"
    if reporting_bump["kind"] in {"minor", "major"} and reporting_bump.get("warning"):
        notes = f"{notes}. {reporting_bump['warning']}"

    saved = save_reporting_pack(
        settings,
        pack_id=settings.dna_config_id,
        version=reporting_version,
        reporting=reporting,
        status=reporting_status,
    )
    state["pack_id"] = settings.dna_config_id
    _append_portal_governance_history(
        state,
        version=reporting_version,
        status=reporting_status,
        approver=approver,
        target="reporting",
        notes=notes,
    )
    if pin_production:
        state["active_reporting_version"] = reporting_version
        if not state.get("active_version"):
            state["active_version"] = reporting_base
    save_workflow_state(settings, state)
    return {
        "status": "saved",
        "target": "reporting",
        "reporting_version": reporting_version,
        "version": reporting_version,
        "approval_status": reporting_status,
        "bump_kind": reporting_bump["kind"],
        "warning": reporting_bump.get("warning") or "",
        "reporting_base_version": reporting_base,
        "reporting_next_patch": bump_patch_version(reporting_base),
        "reporting_next_minor": bump_minor_version(reporting_base),
        "reporting_next_major": bump_major_version(reporting_base),
        "reporting_path": saved.get("path"),
    }


def save_governance_packs_from_portal(
    settings: DnaSettings,
    *,
    dna_yaml: str,
    reporting_yaml: str,
    dna_version: str,
    reporting_version: str,
    pin_production: bool,
    approver: str,
) -> dict[str, Any]:
    """Persist DNA and reporting from portal editors (combined save helper)."""
    from hiveflow.dna.governance import save_governance_version
    from hiveflow.dna.schema import load_definition_pack_yaml
    from hiveflow.dna.web.portal.governance_helpers.proposals import (
        bump_major_version,
        bump_minor_version,
        bump_patch_version,
        classify_manual_version_bump,
    )
    from hiveflow.dna.reporting import (
        load_reporting_pack_yaml,
        normalize_reporting_identity,
    )
    from hiveflow.dna.workflow import load_workflow_state, save_workflow_state

    dna_version = dna_version.strip()
    reporting_version = reporting_version.strip()
    if dna_version != reporting_version:
        dna_result = save_governance_dna_from_portal(
            settings,
            dna_yaml=dna_yaml,
            dna_version=dna_version,
            pin_production=pin_production,
            approver=approver,
        )
        reporting_result = save_governance_reporting_from_portal(
            settings,
            reporting_yaml=reporting_yaml,
            reporting_version=reporting_version,
            pin_production=pin_production,
            approver=approver,
        )
        warnings = " ".join(
            part
            for part in (dna_result.get("warning") or "", reporting_result.get("warning") or "")
            if part
        ).strip()
        return {
            "status": "saved",
            "dna_version": dna_result["dna_version"],
            "reporting_version": reporting_result["reporting_version"],
            "version": dna_result["dna_version"],
            "approval_status": dna_result["approval_status"],
            "bump_kind": dna_result["bump_kind"],
            "warning": warnings,
            "dna_base_version": dna_result["dna_base_version"],
            "reporting_base_version": reporting_result["reporting_base_version"],
            "dna_next_patch": dna_result["dna_next_patch"],
            "dna_next_minor": dna_result["dna_next_minor"],
            "dna_next_major": dna_result["dna_next_major"],
            "reporting_next_patch": reporting_result["reporting_next_patch"],
            "reporting_next_minor": reporting_result["reporting_next_minor"],
            "reporting_next_major": reporting_result["reporting_next_major"],
            "dna_path": dna_result.get("dna_path"),
            "reporting_path": reporting_result.get("reporting_path"),
            "manifest_path": dna_result.get("manifest_path"),
        }

    state = load_workflow_state(settings, settings.dna_config_id)
    pack = load_definition_pack_yaml(dna_yaml)
    pack.pack_id = settings.dna_config_id
    dna_base = str(state.get("active_version") or pack.version or "")
    reporting_base = str(
        state.get("active_reporting_version") or state.get("active_version") or ""
    )

    dna_bump = classify_manual_version_bump(dna_base, dna_version)
    if dna_bump["kind"] == "invalid":
        raise ValueError(f"DNA: {dna_bump['error']}")
    reporting_bump = classify_manual_version_bump(reporting_base, dna_version)
    if reporting_bump["kind"] == "invalid":
        raise ValueError(f"Reporting: {reporting_bump['error']}")

    pack.version = dna_version
    reporting = load_reporting_pack_yaml(reporting_yaml)
    if pin_production:
        pack.status = "production"
        pack.approval.status = "production"
        pack.approval.approver = approver or pack.approval.approver or "Portal admin"
        pack.approval.approved_at = datetime.now(UTC).date().isoformat()
        reporting_status = "production"
    else:
        pack.status = "draft"
        pack.approval.status = "draft"
        reporting_status = "draft"

    reporting = normalize_reporting_identity(
        settings,
        reporting,
        version=dna_version,
        status=reporting_status,
    )

    warnings: list[str] = []
    for bump in (dna_bump, reporting_bump):
        if bump["kind"] in {"minor", "major"} and bump.get("warning"):
            warnings.append(str(bump["warning"]))

    saved = save_governance_version(settings, pack=pack, reporting=reporting)
    state["pack_id"] = settings.dna_config_id
    notes = "Updated via client portal"
    if warnings:
        notes = f"{notes}. {' '.join(warnings)}"
    _append_portal_governance_history(
        state,
        version=dna_version,
        status=pack.approval.status,
        approver=approver,
        target="all",
        notes=notes,
    )
    if pin_production:
        state["active_version"] = dna_version
        state["active_reporting_version"] = dna_version
    save_workflow_state(settings, state)
    return {
        "status": "saved",
        "dna_version": dna_version,
        "reporting_version": dna_version,
        "version": dna_version,
        "approval_status": pack.approval.status,
        "bump_kind": dna_bump["kind"],
        "warning": " ".join(warnings),
        "dna_base_version": dna_base,
        "reporting_base_version": reporting_base,
        "dna_next_patch": bump_patch_version(dna_base),
        "dna_next_minor": bump_minor_version(dna_base),
        "dna_next_major": bump_major_version(dna_base),
        "reporting_next_patch": bump_patch_version(reporting_base),
        "reporting_next_minor": bump_minor_version(reporting_base),
        "reporting_next_major": bump_major_version(reporting_base),
        **{k: saved[k] for k in ("dna_path", "reporting_path", "manifest_path") if k in saved},
    }






def render_kpi_generator(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    proposal: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    message: str = "",
    error: str = "",
    active_tab: str = "generator",
    pending_drafts: list[dict[str, Any]] | None = None,
    approved_drafts: list[dict[str, Any]] | None = None,
    refresh_status: dict[str, Any] | None = None,
    refresh_quota: dict[str, Any] | None = None,
) -> Response:
    from hiveflow.dna.web.portal.governance_helpers.bedrock_usage import usage_summary as bedrock_usage_summary
    from hiveflow.dna.web.portal.kpi_generator.render import render_kpi_generator_body

    url: Callable[[str], str] = lambda path: _app_url(request, path)
    usage = None
    if is_admin:
        usage = bedrock_usage_summary(
            settings,
            client_id=client.client_id,
            monthly_budget_usd=client.config_assistant_monthly_budget_usd,
        ).to_dict()
    body = page_header(
        "DNA Engine",
        "Natural language KPIs backed by version-pinned Athena SQL. "
        "Uses Source Browser gold YAML as reference; approved SQL is replayed verbatim on refresh.",
        eyebrow="Agents",
    )
    body += render_kpi_generator_body(
        settings=settings,
        url=url,
        is_admin=is_admin,
        proposal=proposal,
        validation=validation,
        message=message,
        error=error,
        usage=usage,
        refresh_status=refresh_status,
        refresh_quota=refresh_quota,
        active_tab=active_tab,
        pending_drafts=pending_drafts,
        approved_drafts=approved_drafts,
    )
    return _html_response(
        request,
        client=client,
        title="DNA Engine",
        active_path=KPI_GENERATOR_ROOT,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )


def render_governance(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    message: str = "",
    error: str = "",
) -> Response:
    from hiveflow.dna.reporting import (
        default_reporting_pack,
        load_production_reporting,
    )
    from hiveflow.dna.workflow import load_production_pack

    try:
        pack = load_production_pack(settings)
    except Exception:  # noqa: BLE001
        pack = load_pack_from_settings(settings)

    workflow = load_workflow_state(settings, settings.dna_config_id)
    manifest = read_json_artifact(settings, f"{settings.gold_dna_prefix}/manifest.json") or {}
    active_version = workflow.get("active_version") or pack.version
    history = workflow.get("history", [])
    if not isinstance(history, list):
        history = []

    try:
        reporting = load_production_reporting(settings)
    except FileNotFoundError:
        reporting = default_reporting_pack(
            pack_id=settings.reporting_config_id,
            version=str(active_version),
            status="draft",
            description="Reporting config not seeded yet.",
        )
    active_reporting_version = (
        workflow.get("active_reporting_version") or reporting.get("version") or active_version
    )

    url: Callable[[str], str] = lambda path: _app_url(request, path)
    dna_history = _filter_pack_history(history, "dna")
    reporting_history = _filter_pack_history(history, "reporting")
    governance_form_action = url("/portal/governance")
    dna_history_rows = _history_table_rows(
        dna_history,
        pack_kind="dna",
        active_version=str(active_version or ""),
        is_admin=is_admin,
        form_action=governance_form_action,
        settings=settings,
    )
    reporting_history_rows = _history_table_rows(
        reporting_history,
        pack_kind="reporting",
        active_version=str(active_reporting_version or ""),
        is_admin=is_admin,
        form_action=governance_form_action,
        settings=settings,
    )
    reporting_page_count, reporting_visualization_count = _reporting_pack_counts(reporting)
    reporting_approver, reporting_approved = _history_approval_for_version(
        reporting_history,
        str(active_reporting_version or ""),
    )
    dna_approver, dna_approved = _history_approval_for_version(
        dna_history,
        str(active_version or ""),
    )
    if dna_approver == "—":
        dna_approver = pack.approval.approver or "—"
    if dna_approved == "—" and pack.approval.approved_at:
        dna_approved = _format_datetime_minute(pack.approval.approved_at)
    manifest_outputs = manifest.get("outputs", [])
    output_count = len(manifest_outputs) if isinstance(manifest_outputs, list) else 0
    published_at = (
        _format_datetime_minute(manifest.get("published_at"))
        if manifest.get("published_at")
        else "—"
    )

    body = page_header(
        "Pack Registry",
        "Version-controlled DNA and reporting packs — view and update the contracts that power this portal.",
        eyebrow="Governance",
    )
    if message:
        body += f'<div class="form-success">{escape(message)}</div>'
    if error:
        body += f'<div class="form-error">{escape(error)}</div>'
    body += render_template(
        "portal/_governance_packs.html",
        dna={
            "description": pack.description,
            "pack_id": pack.pack_id,
            "version": pack.version,
            "status": pack.approval.status,
            "active_version": str(active_version),
            "approver": dna_approver,
            "approved": dna_approved,
            "published_at": published_at,
            "output_count": output_count,
        },
        reporting={
            "description": str(reporting.get("description") or ""),
            "pack_id": str(reporting.get("pack_id") or settings.reporting_config_id),
            "version": str(reporting.get("version") or "—"),
            "status": str(reporting.get("status") or "—"),
            "active_version": str(active_reporting_version),
            "approver": reporting_approver,
            "approved": reporting_approved,
            "page_count": reporting_page_count,
            "visualization_count": reporting_visualization_count,
        },
        kpi_generator_url=url("/portal/dna/kpi-generator"),
    )
    body += render_template(
        "portal/_governance_history.html",
        is_admin=is_admin,
        dna_history_rows=Markup(dna_history_rows),
        reporting_history_rows=Markup(reporting_history_rows),
    )
    return _html_response(
        request,
        client=client,
        title="Pack Registry",
        active_path="/portal/governance",
        body=body,
        is_admin=is_admin,
        settings=settings,
    )




def _user_status_label(status: str) -> str:
    labels = {
        "CONFIRMED": "Active",
        "FORCE_CHANGE_PASSWORD": "Invite pending",
        "RESET_REQUIRED": "Password reset",
        "UNCONFIRMED": "Unconfirmed",
    }
    return labels.get(status, status.replace("_", " ").title())


def _legacy_portal_users(client_id: str, *, company: str, environment: str) -> list[Any]:
    from hiveflow.dna.web.portal.auth_login import load_portal_users
    from hiveflow.dna.web.portal.cognito import PORTAL_ROLE_ADMIN, PortalUserRecord

    normalized = client_id.strip().lower()
    records: list[PortalUserRecord] = []
    for user in load_portal_users(company=company, environment=environment).values():
        if user.client_id != normalized:
            continue
        records.append(
            PortalUserRecord(
                username=user.username,
                email="",
                client_id=user.client_id,
                role=PORTAL_ROLE_ADMIN,
                status="CONFIRMED",
                enabled=True,
            )
        )
    records.sort(key=lambda item: item.username)
    return records


def render_admin_users(
    request: Request,
    *,
    client: ClientPortalConfig,
    users: list[Any],
    current_username: str,
    message: str = "",
    error: str = "",
    invites_enabled: bool = True,
    is_admin: bool = True,
    settings: DnaSettings | None = None,
) -> Response:
    from hiveflow.dna.web.portal.cognito import PORTAL_ROLE_ADMIN, PORTAL_ROLE_MEMBER

    url: Callable[[str], str] = lambda path: _app_url(request, path)
    seat_count = len(users)
    at_capacity = seat_count >= client.max_users
    users_path = "/portal/governance/users"

    message_html = f'<div class="form-success">{escape(message)}</div>' if message else ""
    error_html = f'<div class="form-error">{escape(error)}</div>' if error else ""

    user_rows = []
    for user in users:
        you = user.username.casefold() == current_username.strip().casefold()
        enabled_label = "Active" if user.enabled else "Disabled"
        if user.status == "FORCE_CHANGE_PASSWORD":
            enabled_label = "Invite pending"
        current_role = str(getattr(user, "role", PORTAL_ROLE_MEMBER) or PORTAL_ROLE_MEMBER).lower()
        user_rows.append(
            {
                "username": user.username,
                "you": you,
                "email": user.email or "—",
                "editable_role": is_admin and not you,
                "current_role": current_role,
                "role_label": current_role.title(),
                "status_label": _user_status_label(user.status),
                "enabled_label": enabled_label,
            }
        )

    invite_disabled = at_capacity or not invites_enabled or not is_admin
    invite_note_kind = None
    if not is_admin:
        invite_note_kind = "not_admin"
    elif not invites_enabled:
        invite_note_kind = "no_cognito"
    elif at_capacity:
        invite_note_kind = "at_capacity"

    body = page_header(
        "Users",
        "Portal users for this client — invite colleagues and manage admin vs member roles.",
        eyebrow="Governance",
    )
    body += badge_row((f"{seat_count} of {client.max_users} seats used", False))
    body += message_html
    body += error_html
    body += render_template(
        "portal/_admin_users.html",
        user_rows=user_rows,
        users_path_url=url(users_path),
        invite_disabled=invite_disabled,
        invite_note_kind=invite_note_kind,
        max_users=client.max_users,
        role_member=PORTAL_ROLE_MEMBER,
        role_admin=PORTAL_ROLE_ADMIN,
    )
    return _html_response(
        request,
        client=client,
        title="Users",
        active_path=users_path,
        body=body,
        is_admin=is_admin,
        settings=settings,
    )






def render_source_docs_inspector(
    request: Request,
    *,
    settings: DnaSettings,
    client: ClientPortalConfig,
    is_admin: bool = False,
    message: str = "",
    error: str = "",
    source: str | None = None,
    configured_sources: list[str] | None = None,
) -> Response:
    from hiveflow.dna.web.portal.semantics.source_docs_render import (
        render_source_docs_inspector_page,
    )

    return render_source_docs_inspector_page(
        request,
        settings=settings,
        client=client,
        is_admin=is_admin,
        html_response=_html_response,
        message=message,
        error=error,
        source=source,
        configured_sources=configured_sources,
    )
