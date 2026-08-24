"""HTML rendering for the Model Mapping page."""

from __future__ import annotations

from typing import Any, Callable

from hiveflow.dna.industry_mapping import ClientModelMapping
from hiveflow.dna.industry_templates import IndustryTemplate
from hiveflow.dna.web.templating import render_template

ROOT_PATH = "/portal/dna/model-mapping"


def _entity_rows(
    template: IndustryTemplate,
    mapping: ClientModelMapping,
    completion: dict[str, Any],
    *,
    url: Callable[[str], str],
) -> list[dict[str, Any]]:
    by_entity = completion.get("by_entity") or {}
    rows = []
    for entity_mapping in mapping.entities:
        stat = by_entity.get(entity_mapping.entity_id) or {"required": 0, "approved": 0, "percent": 100.0}
        silver_binding = ""
        if entity_mapping.silver_source and entity_mapping.silver_entity:
            silver_binding = f"{entity_mapping.silver_source}.{entity_mapping.silver_entity}"
        rows.append(
            {
                "entity_id": entity_mapping.entity_id,
                "included": entity_mapping.included,
                "silver_binding": silver_binding,
                "percent": stat["percent"],
                "approved": stat["approved"],
                "required": stat["required"],
                "view_url": url(f"{ROOT_PATH}?entity={entity_mapping.entity_id}"),
            }
        )
    return rows


def _field_rows(template: IndustryTemplate, entity_mapping) -> list[dict[str, Any]]:
    try:
        template_entity = template.entity_by_id(entity_mapping.entity_id)
    except KeyError:
        template_entity = None

    rows = []
    for fm in entity_mapping.fields:
        field_spec = None
        if template_entity is not None:
            try:
                field_spec = template_entity.field_by_id(fm.field_id)
            except KeyError:
                field_spec = None
        rows.append(
            {
                "field_id": fm.field_id,
                "display_name": field_spec.display_name if field_spec else "",
                "required": bool(field_spec.required) if field_spec else False,
                "silver_column": fm.silver_column,
                "confidence": f"{fm.confidence:.0%}" if fm.confidence else "—",
                "status": fm.status,
            }
        )
    return rows


def render_model_mapping_page(
    *,
    url: Callable[[str], str],
    mapping: ClientModelMapping | None,
    template: IndustryTemplate | None,
    completion: dict[str, Any] | None,
    selected_entity: str = "",
    promote_report: dict[str, Any] | None = None,
    available: list[str] | None = None,
) -> str:
    action_url = url(ROOT_PATH)

    if mapping is None or template is None:
        picker_html = render_template(
            "portal/model_mapping/_industry_picker.html",
            available=available or [],
            action_url=action_url,
        )
        return f'<div class="model-mapping-page"><section class="section"><div class="card">{picker_html}</div></section></div>'

    completion = completion or {"overall_percent": 100.0, "by_entity": {}}
    entity_rows = _entity_rows(template, mapping, completion, url=url)
    list_html = render_template(
        "portal/model_mapping/_entity_list.html",
        entities=entity_rows,
        action_url=action_url,
        industry_pack_id=mapping.industry_pack_id,
        industry_version=mapping.industry_version,
        overall_percent=completion["overall_percent"],
    )
    body = f'<section class="section"><div class="card">{list_html}</div></section>'

    if selected_entity:
        try:
            entity_mapping = mapping.entity_by_id(selected_entity)
        except KeyError:
            entity_mapping = None
        if entity_mapping is not None:
            field_html = render_template(
                "portal/model_mapping/_field_list.html",
                fields=_field_rows(template, entity_mapping),
                entity_id=selected_entity,
                action_url=action_url,
            )
            body += f'<section class="section"><div class="card">{field_html}</div></section>'

    promote_html = render_template(
        "portal/model_mapping/_promote_panel.html", action_url=action_url, report=promote_report
    )
    body += f'<section class="section"><div class="card">{promote_html}</div></section>'
    return f'<div class="model-mapping-page">{body}</div>'
