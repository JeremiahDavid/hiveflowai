"""HTML rendering for the Data Profile Explorer page."""

from __future__ import annotations

from typing import Any, Callable

from hiveflow.dna.web.templating import render_template
from hiveflow.dna.web.theme import empty_state, escape

_DETAIL_PATH = "/portal/dna/data-profile"


def detail_url(url: Callable[[str], str], source: str, entity: str) -> str:
    return url(f"{_DETAIL_PATH}/{source}/{entity}")


def _index_table_html(rows: list[dict[str, Any]], *, url: Callable[[str], str]) -> str:
    table_rows = [{**row, "detail_url": detail_url(url, row["source"], row["entity"])} for row in rows]
    return render_template("portal/data_profile/_index_table.html", rows=table_rows)


def render_data_profile_index_page(*, url: Callable[[str], str], rows: list[dict[str, Any]]) -> str:
    sources = sorted({row["source"] for row in rows})
    refresh_forms = "".join(
        f"""
        <form method="post" action="{escape(url(f'{_DETAIL_PATH}/{source}/refresh'))}" class="profile-source-refresh-form">
          <button type="submit" class="btn btn-secondary">Re-profile all {escape(source)} tables</button>
        </form>
        """
        for source in sources
    )
    return f"""
    <section class="section">
      <div class="card">
        {refresh_forms}
        {_index_table_html(rows, url=url)}
      </div>
    </section>
    """


def _field_rows(profile: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for col in profile.get("fields") or []:
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
                "description": str(col.get("description") or ""),
                "description_edited": bool(col.get("description_edited")),
            }
        )
    return rows


def render_data_profile_detail_page(
    *, url: Callable[[str], str], source: str, entity: str, profile: dict[str, Any] | None
) -> str:
    if profile is None:
        body = empty_state(
            "Not profiled yet",
            f"{source}.{entity} has not been sampled and described yet.",
        )
        action_url = detail_url(url, source, entity)
        body += f"""
        <form method="post" action="{escape(action_url)}">
          <input type="hidden" name="action" value="refresh_entity" />
          <button type="submit" class="btn btn-primary">Profile now</button>
        </form>
        """
        return f'<section class="section"><div class="card">{body}</div></section>'

    detail_html = render_template(
        "portal/data_profile/_profile_detail.html",
        action_url=detail_url(url, source, entity),
        purpose=str(profile.get("purpose") or ""),
        purpose_edited=bool(profile.get("purpose_edited")),
        confidence=f"{float(profile.get('confidence') or 0):.0%}",
        row_count=int(profile.get("row_count") or 0),
        last_profiled_at=str(profile.get("last_profiled_at") or ""),
        notes=[str(n) for n in (profile.get("notes") or []) if str(n).strip()],
        fields=_field_rows(profile),
    )
    return f'<section class="section"><div class="card">{detail_html}</div></section>'
