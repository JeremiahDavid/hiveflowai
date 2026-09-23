"""Rendering seam between DNA Engine's FastAPI routes and the shell's
existing ``hiveflow.dna.web.portal.views`` render functions.

Every ``render_*`` function in ``portal.views`` (and the ``portal.semantics``
page it delegates to) already only ever reads one attribute off the
``request`` object it's given — ``request.script_root``, used to build
API-Gateway-stage-prefixed links (see ``routing_helpers._app_url``). DNA
Engine has no stage prefix to strip (same as Spreadsheet Engine's simpler
``url()`` handling), so a request exposing an empty ``script_root`` is a
complete, correct stand-in — there is no need to duplicate ~1700 lines of
rendering logic into this package. This module is that shim, plus the
werkzeug-Response -> Starlette-Response conversion every route needs at the
end of a call into ``portal.views``.
"""

from __future__ import annotations

from starlette.responses import Response as StarletteResponse


class ScriptRootRequest:
    """Duck-typed stand-in for the werkzeug ``Request`` the shell's
    ``portal.views``/``portal.semantics`` render functions expect — DNA
    Engine has no API Gateway stage prefix to strip, so ``script_root`` is
    always empty."""

    script_root = ""


REQUEST = ScriptRootRequest()

# In-page sub-nav under Governance (mirrors portal.views.GOVERNANCE_SECTION_NAV
# — DNA Engine's own paths, unprefixed since it's the host now).
GOVERNANCE_SECTION_NAV = (
    ("/governance", "Pack Registry"),
    ("/governance/users", "Users"),
)


def to_starlette_response(werkzeug_response) -> StarletteResponse:
    """Convert a werkzeug ``Response`` (as returned by every reused
    ``portal.views``/``portal.semantics`` render function) into a Starlette
    ``Response`` a FastAPI route can return."""
    skip = {"content-length", "content-type"}
    headers = {key: value for key, value in werkzeug_response.headers.items() if key.lower() not in skip}
    return StarletteResponse(
        content=werkzeug_response.get_data(),
        status_code=werkzeug_response.status_code,
        headers=headers,
        media_type=werkzeug_response.mimetype,
    )
