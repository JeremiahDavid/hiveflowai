"""Own Jinja2 environment for Spreadsheet Lab.

Deliberately not shared with ``hiveflow.dna.web.templating`` — this UI never
imports the production portal's web internals (see the package docstring).
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, select_autoescape

_env = Environment(
    loader=PackageLoader("hiveflow.spreadsheet_lab.web", "templates"),
    autoescape=select_autoescape(["html", "jinja", "jinja2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(template_name: str, **context: object) -> str:
    return _env.get_template(template_name).render(**context)
