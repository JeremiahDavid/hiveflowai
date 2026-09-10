"""Resolve portal pages from the company reporting config."""

from __future__ import annotations

import logging
from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.reporting_render import page_has_content
from hiveflow.dna.reporting import (
    default_reporting_pack,
    load_production_reporting,
    load_reporting_boilerplate,
)

_logger = logging.getLogger("hiveflow.portal.reporting")

# Companies whose reporting sidecar this container has already tried to seed.
_seeded_reporting_configs: set[str] = set()


def _lazy_seed_reporting_config(settings: DnaSettings) -> None:
    """Best-effort one-time seed of ``{company}_reporting_config.yaml``.

    New clients get it from ``DnaStack``'s governance init; this covers companies
    whose DnaStack predates that. Idempotent (``ensure_reporting_config`` returns
    ``skipped`` when present); runs under the request's tenant credentials.
    """
    company = (settings.company or "").strip().lower()
    if not company or not settings.s3_bucket or company in _seeded_reporting_configs:
        return
    _seeded_reporting_configs.add(company)
    try:
        from hiveflow.dna.init_client import ensure_reporting_config

        ensure_reporting_config(settings)
    except Exception:  # noqa: BLE001 — never block a render on the seed
        _logger.warning("lazy reporting-config seed failed for %s", company, exc_info=True)

# Side-nav entry: (path, title) or (path, title, children) where children are leaf tuples.
SideNavItem = tuple[str, str] | tuple[str, str, tuple[tuple[str, str], ...]]

CHART_CATALOG_PAGE_ID = "page_chart_catalog"
CHART_CATALOG_PATH = "/portal/chart-demo"
_LEGACY_CHART_PAGE_IDS = frozenset({CHART_CATALOG_PAGE_ID, "page_chart_demo"})


def _normalize_portal_path(path: str | None, page_id: str) -> str:
    value = (path or "").strip()
    if not value:
        value = f"/portal/{page_id.strip().lower()}"
    if not value.startswith("/"):
        value = f"/{value}"
    if value != "/" and value.endswith("/"):
        value = value.rstrip("/")
    return value


def load_reporting_layout(
    settings: DnaSettings,
    *,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pinned production reporting config, or boilerplate fallback for local tooling."""
    if override is not None:
        return override
    try:
        return load_production_reporting(settings)
    except FileNotFoundError:
        _lazy_seed_reporting_config(settings)
        try:
            return load_production_reporting(settings)
        except FileNotFoundError:
            pass
        try:
            return load_reporting_boilerplate(
                pack_id=settings.reporting_config_id,
                version="1.0.0",
            )
        except Exception:  # noqa: BLE001
            return default_reporting_pack(
                pack_id=settings.reporting_config_id,
                version="1.0.0",
                status="draft",
            )


def chart_catalog_enabled(layout: dict[str, Any]) -> bool:
    return bool(layout.get("include_chart_catalog"))


def chart_catalog_page() -> dict[str, Any]:
    """Synthetic page entry — content is rendered by the hardcoded chart catalog gallery."""
    return {
        "id": CHART_CATALOG_PAGE_ID,
        "title": "Chart catalog",
        "path": CHART_CATALOG_PATH,
        "description": "Interactive chart gallery for supported reporting chart types",
        "pillar": "developer",
        "chart_catalog": True,
    }


def is_chart_catalog_page(page: dict[str, Any] | None) -> bool:
    if not page:
        return False
    if page.get("chart_catalog"):
        return True
    page_id = str(page.get("id") or "")
    if page_id in _LEGACY_CHART_PAGE_IDS:
        return True
    return _normalize_portal_path(str(page.get("path") or ""), page_id) == CHART_CATALOG_PATH


def list_reporting_pages(
    settings: DnaSettings,
    *,
    override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    layout = load_reporting_layout(settings, override=override)
    pages: list[dict[str, Any]] = []
    for raw in layout.get("pages", []):
        if not isinstance(raw, dict):
            continue
        page_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not page_id or not title:
            continue
        if page_id in _LEGACY_CHART_PAGE_IDS or _normalize_portal_path(
            str(raw.get("path") or ""), page_id
        ) == CHART_CATALOG_PATH:
            continue
        page = dict(raw)
        page["id"] = page_id
        page["title"] = title
        page["path"] = _normalize_portal_path(str(raw.get("path") or ""), page_id)
        page["description"] = str(raw.get("description") or "")
        pages.append(page)
    if chart_catalog_enabled(layout):
        pages.append(chart_catalog_page())
    return pages


def find_reporting_page(
    settings: DnaSettings,
    path: str,
    *,
    override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    target = _normalize_portal_path(path, "page")
    for page in list_reporting_pages(settings, override=override):
        if page["path"] == target:
            return page
    return None


def _pillar_hub_path(pages: list[dict[str, Any]], pillar: str) -> str | None:
    """Hub for a pillar: the content-less landing page owned by that pillar."""
    for page in pages:
        if str(page.get("pillar") or "") != pillar:
            continue
        if page_has_content(page):
            continue
        path = str(page.get("path") or "")
        if path in {"/portal", "/portal/"}:
            continue
        return path
    return None


def reporting_data_menu(
    settings: DnaSettings,
    *,
    override: dict[str, Any] | None = None,
) -> tuple[SideNavItem, ...]:
    """Flat top-level items with detail pages nested under their pillar hub.

    Content pages that share a pillar with a hub never appear as top-level orphans.
    """
    pages = list_reporting_pages(settings, override=override)
    hub_by_pillar: dict[str, str] = {}
    for page in pages:
        pillar = str(page.get("pillar") or "").strip()
        if not pillar or pillar in hub_by_pillar:
            continue
        hub_path = _pillar_hub_path(pages, pillar)
        if hub_path:
            hub_by_pillar[pillar] = hub_path

    children_by_hub: dict[str, list[tuple[str, str]]] = {}
    nested_paths: set[str] = set()
    for page in pages:
        pillar = str(page.get("pillar") or "").strip()
        hub_path = hub_by_pillar.get(pillar)
        path = str(page.get("path") or "")
        if not hub_path or path == hub_path or not page_has_content(page):
            continue
        children_by_hub.setdefault(hub_path, []).append((path, str(page["title"])))
        nested_paths.add(path)

    menu: list[SideNavItem] = []
    for page in pages:
        path = str(page["path"])
        if path in nested_paths:
            continue
        children = tuple(children_by_hub.get(path) or ())
        if children:
            menu.append((path, str(page["title"]), children))
        else:
            menu.append((path, str(page["title"])))
    return tuple(menu)


def flatten_side_nav_paths(items: tuple[SideNavItem, ...] | tuple[tuple[str, str], ...]) -> set[str]:
    paths: set[str] = set()
    for item in items:
        paths.add(item[0])
        if len(item) > 2:
            for child_path, _title in item[2]:  # type: ignore[misc]
                paths.add(child_path)
    return paths


def reporting_quick_links(
    settings: DnaSettings,
    *,
    override: dict[str, Any] | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Overview quick links — all configured pages except the summary/home page."""
    links: list[tuple[str, str, str]] = []
    for page in list_reporting_pages(settings, override=override):
        if page["path"] in {"/portal", "/portal/"}:
            continue
        links.append((page["path"], page["title"], page.get("description") or ""))
    return tuple(links)


def page_source_output(
    page: dict[str, Any] | None,
    *,
    kind: str = "table",
    default: str,
) -> str:
    if not page:
        return default
    key = "tables" if kind == "table" else "charts"
    items = page.get(key) or []
    if not isinstance(items, list):
        return default
    for item in items:
        if isinstance(item, dict):
            source = str(item.get("source_output") or "").strip()
            if source:
                return source
    return default
