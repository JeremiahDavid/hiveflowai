"""DNA portal section navigation — source browser, DNA Engine, catalog."""

from __future__ import annotations

import os
from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.catalog import (
    CATALOG_ROOT,
    catalog_table_label,
    list_catalog_tables,
)

DNA_ROOT = "/dna"
KPI_GENERATOR_ROOT = f"{DNA_ROOT}/kpi-generator"
SOURCE_DOCS_INSPECTOR_ROOT = "/semantics/source-docs"
DATA_PROFILE_ROOT = f"{DNA_ROOT}/data-profile"
MODEL_MAPPING_ROOT = f"{DNA_ROOT}/model-mapping"

_SOURCE_BROWSER_LABEL = "Source Browser"
_KPI_GENERATOR_LABEL = "KPI Generator"
_DNA_CATALOG_LABEL = "DNA Catalog"
_DATA_PROFILE_LABEL = "Data Profile"
_MODEL_MAPPING_LABEL = "Model Mapping"

SideNavItem = (
    tuple[str, str]
    | tuple[str, str, tuple[Any, ...]]
    | tuple[str, str, tuple[Any, ...], str]
)


_SOURCE_LABELS = {
    "dbc": "Business Central",
    "qbo": "QuickBooks Online",
    "qbd": "QuickBooks Desktop",
}


def source_label(source: str) -> str:
    key = source.strip().lower()
    return _SOURCE_LABELS.get(key, key.replace("_", " ").title() or "Source")


def source_docs_inspector_path(source: str | None = None) -> str:
    key = (source or "").strip().lower()
    if not key:
        return SOURCE_DOCS_INSPECTOR_ROOT
    return f"{SOURCE_DOCS_INSPECTOR_ROOT}/{key}"


def _catalog_nav_children(settings: DnaSettings) -> tuple[tuple[str, str], ...]:
    return tuple(
        (f"{CATALOG_ROOT}/{output.id}", catalog_table_label(output))
        for output in list_catalog_tables(settings)
    )


def dna_engine_origin() -> str:
    """Absolute origin for the DNA Engine subdomain (e.g.
    ``https://dna-engine.hive-flow-ai.com``), derived from the same
    ``HIVEFLOW_PORTAL_COOKIE_DOMAIN`` the Lambda already carries for
    cross-subdomain session sharing. Empty when that isn't configured
    (local/dev with no multi-tenant domain wiring). Used only by the portal
    shell, which no longer serves any DNA/Agents/Governance route itself —
    DNA Engine's own pages call ``agents_section_nav()``/``dna_section_nav()``
    directly and get same-origin relative hrefs back, same as the shell did
    before this split."""
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip()
    if not cookie_domain:
        return ""
    return f"https://dna-engine{cookie_domain}"


def dna_engine_site_url(path: str) -> str:
    """``path`` resolved against the DNA Engine origin, or unchanged (relative)
    when no cookie domain is configured — matches ``_client_reporting_site_url``'s
    graceful local-dev fallback."""
    origin = dna_engine_origin()
    return f"{origin}{path}" if origin else path


def _spreadsheet_engine_nav_items() -> tuple[SideNavItem, ...]:
    """Link to the Spreadsheet Engine's own subdomain (see
    infra/spreadsheet_engine.py) — it's no longer a tab inside Source
    Browser, just a portal-authenticated sibling app. Derived from the same
    ``HIVEFLOW_PORTAL_COOKIE_DOMAIN`` the Lambda already carries for
    cross-subdomain session sharing; omitted when that isn't configured
    (local/dev with no multi-tenant domain wiring)."""
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip()
    if not cookie_domain:
        return ()
    return (
        (f"https://spreadsheet-engine{cookie_domain}/", "Spreadsheet Engine", (), "spreadsheet"),
    )


def agents_section_nav() -> tuple[SideNavItem, ...]:
    """DNA Engine + Spreadsheet Engine — the Agents pillar between DNA and
    Governance. Each item carries an icon key (4th tuple element) rendered
    beside its label in the sidebar."""
    return (
        (KPI_GENERATOR_ROOT, _KPI_GENERATOR_LABEL, (), "dna"),
        *_spreadsheet_engine_nav_items(),
    )


def dna_section_nav(settings: DnaSettings | None) -> tuple[Any, ...]:
    if settings is None:
        return (
            (SOURCE_DOCS_INSPECTOR_ROOT, _SOURCE_BROWSER_LABEL),
            (CATALOG_ROOT, _DNA_CATALOG_LABEL),
            (DATA_PROFILE_ROOT, _DATA_PROFILE_LABEL),
            (MODEL_MAPPING_ROOT, _MODEL_MAPPING_LABEL),
        )

    catalog_children = _catalog_nav_children(settings)
    catalog_item: SideNavItem = (
        (CATALOG_ROOT, _DNA_CATALOG_LABEL, catalog_children)
        if catalog_children
        else (CATALOG_ROOT, _DNA_CATALOG_LABEL)
    )
    return (
        (SOURCE_DOCS_INSPECTOR_ROOT, _SOURCE_BROWSER_LABEL),
        catalog_item,
        (DATA_PROFILE_ROOT, _DATA_PROFILE_LABEL),
        (MODEL_MAPPING_ROOT, _MODEL_MAPPING_LABEL),
    )
