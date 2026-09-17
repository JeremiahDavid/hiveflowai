"""Small, pure WSGI helpers shared by every route module (public/portal/admin).

Extracted from app.py's create_app() during the Phase 1 monolith split — these
never closed over any create_app state, so the move is behavior-identical.
"""

from __future__ import annotations

import json
from typing import Any

from werkzeug.wrappers import Request, Response


def _json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2, default=str),
        status=status,
        mimetype="application/json",
    )


def _request_wants_json(request: Request) -> bool:
    if request.headers.get("X-HiveFlow-Inline") == "1":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


def _app_url(request: Request, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        # Already absolute — e.g. a nav link to the Spreadsheet Engine's own
        # subdomain. Don't prefix it with this app's script_root.
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{request.script_root}{path}"


def _redirect(request: Request, path: str) -> Response:
    return Response(status=302, headers={"Location": _app_url(request, path)})
