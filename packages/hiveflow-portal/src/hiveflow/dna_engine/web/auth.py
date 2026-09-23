"""Auth for DNA Engine — trusts the portal shell's session instead of
maintaining a separate login. Same approach as ``hiveflow.spreadsheet_lab.web.auth``
(see its module docstring for the cross-subdomain cookie mechanism); this
module additionally exposes admin-gating, which DNA Engine's governance and
model-mapping write paths need and Spreadsheet Engine's don't.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from hiveflow.dna.web.portal.auth import PortalSession
from hiveflow.dna.web.portal.auth import require_portal_admin as _require_portal_admin
from hiveflow.dna.web.portal.auth import session_from_request as _portal_session_from_request

from hiveflow.dna_engine.web.tenant import hosting_environment


def hosting_company() -> str:
    return os.getenv("HIVEFLOW_COMPANY", "poc").strip() or "poc"


def portal_session_from_request(request: object) -> PortalSession | None:
    return _portal_session_from_request(request, company=hosting_company(), environment=hosting_environment())


def portal_login_url(absolute_next_url: str) -> str:
    primary_site = os.getenv("HIVEFLOW_PRIMARY_SITE_URL", "").strip().rstrip("/")
    login_base = f"{primary_site}/portal/login" if primary_site else "/portal/login"
    return f"{login_base}?next={quote(absolute_next_url, safe='')}"


def is_portal_admin(username: str) -> bool:
    return _require_portal_admin(username, company=hosting_company(), environment=hosting_environment())
