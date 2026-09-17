"""Auth for the Spreadsheet Engine — trusts the real client portal's session
instead of maintaining a separate login.

The Spreadsheet Engine is its own Lambda/app (see ``app.py``'s module
docstring), but it is no longer a sandbox seen only by the build team — it's
a feature of the client portal, just served from its own subdomain. Rather
than duplicate the portal's Cognito-backed login, this module validates the
*same* signed ``hiveflow_portal_session`` cookie the portal issues:

- ``hiveflow.dna.web.portal.auth.session_from_request`` already duck-types
  across werkzeug and Starlette requests (both expose a ``.cookies`` dict),
  so it's reused directly rather than re-implemented.
- The cookie is only sent here at all if the deployment sets
  ``HIVEFLOW_PORTAL_COOKIE_DOMAIN`` to the shared parent zone (e.g.
  ``.hive-flow-ai.com``) on *both* the portal's Lambda and this one, and both
  are configured with the same ``HIVEFLOW_PORTAL_SESSION_SECRET_ARN`` so they
  sign/verify with the identical secret.
- No valid session → redirect to the portal's own login page with ``next``
  pointing back at this (absolute, cross-subdomain) URL.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from hiveflow.dna.web.portal.auth import PortalSession
from hiveflow.dna.web.portal.auth import session_from_request as _portal_session_from_request

from hiveflow.spreadsheet_lab.web.tenant import hosting_company, hosting_environment


def portal_session_from_request(request: object) -> PortalSession | None:
    return _portal_session_from_request(request, company=hosting_company(), environment=hosting_environment())


def portal_login_url(absolute_next_url: str) -> str:
    primary_site = os.getenv("HIVEFLOW_PRIMARY_SITE_URL", "").strip().rstrip("/")
    login_base = f"{primary_site}/portal/login" if primary_site else "/portal/login"
    return f"{login_base}?next={quote(absolute_next_url, safe='')}"
