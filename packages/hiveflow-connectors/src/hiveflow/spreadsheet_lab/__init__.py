"""Spreadsheet Lab: a separate, simplified rebuild of the Spreadsheet Engine.

Deployed to its own sandbox stack/subdomain to validate a new design without
touching the production ``hiveflow.spreadsheet`` pipeline. See
``docs/spreadsheet-lab.md`` for the phase-by-phase workflow.

Hard rule (mirrors ``hiveflow.dna`` / ``hiveflow.dna.web``): this package must
never import ``hiveflow.spreadsheet_lab.web`` — the UI package depends on the
engine, never the reverse.
"""

from __future__ import annotations

from pkgutil import extend_path

# Same namespace-merge trick as hiveflow.dna's __init__.py: the engine lives
# here (hiveflow-connectors), the UI lives in hiveflow.spreadsheet_lab.web
# (hiveflow-portal) — this lets both resolve under one `hiveflow.spreadsheet_lab`
# package across the two separately-installed pip packages.
__path__ = extend_path(__path__, __name__)
