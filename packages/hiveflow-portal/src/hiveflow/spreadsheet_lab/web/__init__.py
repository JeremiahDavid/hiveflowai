"""Spreadsheet Lab UI — a genuinely separate FastAPI app, not mounted into the
existing portal's Werkzeug app (``hiveflow.dna.web``). Depends on the engine
package (``hiveflow.spreadsheet_lab``); the engine never imports this package.
"""

from __future__ import annotations
