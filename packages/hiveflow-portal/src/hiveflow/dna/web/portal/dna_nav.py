"""DNA portal section navigation — source browser, DNA Engine, catalog."""

from __future__ import annotations

from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.catalog import (
    CATALOG_ROOT,
    catalog_table_label,
    list_catalog_tables,
)

DNA_ROOT = "/portal/dna"
KPI_GENERATOR_ROOT = f"{DNA_ROOT}/kpi-generator"
SOURCE_DOCS_INSPECTOR_ROOT = "/portal/semantics/source-docs"
DATA_PROFILE_ROOT = f"{DNA_ROOT}/data-profile"
MODEL_MAPPING_ROOT = f"{DNA_ROOT}/model-mapping"

_SOURCE_BROWSER_LABEL = "Source Browser"
_KPI_GENERATOR_LABEL = "DNA Engine"
_DNA_CATALOG_LABEL = "DNA Catalog"
_DATA_PROFILE_LABEL = "Data Profile"
_MODEL_MAPPING_LABEL = "Model Mapping"

SideNavItem = tuple[str, str] | tuple[str, str, tuple[Any, ...]]


_SOURCE_LABELS = {
    "sse": "Spreadsheet Engine",
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


def dna_section_nav(settings: DnaSettings | None) -> tuple[Any, ...]:
    if settings is None:
        return (
            (SOURCE_DOCS_INSPECTOR_ROOT, _SOURCE_BROWSER_LABEL),
            (KPI_GENERATOR_ROOT, _KPI_GENERATOR_LABEL),
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
        (KPI_GENERATOR_ROOT, _KPI_GENERATOR_LABEL),
        catalog_item,
        (DATA_PROFILE_ROOT, _DATA_PROFILE_LABEL),
        (MODEL_MAPPING_ROOT, _MODEL_MAPPING_LABEL),
    )
