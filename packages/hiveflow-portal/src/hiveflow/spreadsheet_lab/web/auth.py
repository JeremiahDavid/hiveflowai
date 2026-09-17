"""Cookie-based login for the Spreadsheet Lab sandbox site.

Originally HTTP Basic Auth, but API Gateway's Lambda proxy integration
silently renames a Lambda-returned ``WWW-Authenticate`` header to
``x-amzn-Remapped-www-authenticate`` (it's on AWS's reserved-header list for
proxy integrations, since API Gateway uses that header itself for its own
authorizer challenges) — so the browser never sees a real challenge and never
shows its native login popup. Confirmed via a real deployed response, not a
hypothetical. A simple HTML login form + signed session cookie sidesteps the
reserved-header problem entirely, still checked against the same Secrets
Manager ``user:pass`` value.

Deliberately the simplest option — one shared username/password, no per-user
identity, no session store — appropriate for a design-validation sandbox seen
only by the build team. Swap for real per-user auth before this UI is shown
to anyone else (see docs/spreadsheet-lab.md).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from functools import lru_cache

SESSION_COOKIE_NAME = "spreadsheet_lab_session"


def _split(raw: str) -> tuple[str, str] | None:
    if ":" not in raw:
        return None
    user, _, password = raw.partition(":")
    return user, password


@lru_cache(maxsize=1)
def _fetch_secret(secret_arn: str) -> str:
    import boto3

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
    client = boto3.client("secretsmanager", region_name=region)
    return str(client.get_secret_value(SecretId=secret_arn).get("SecretString") or "")


def _resolve_credentials() -> tuple[str, str] | None:
    """``(username, password)`` to require, or ``None`` to disable auth.

    Local dev: set ``HIVEFLOW_SPREADSHEET_LAB_BASIC_AUTH=user:pass``, or leave
    everything unset to run with no auth at all for a laptop loop with no AWS
    credentials. Deployed: ``HIVEFLOW_SPREADSHEET_LAB_AUTH_SECRET_ARN`` names a
    Secrets Manager secret whose string value is ``user:pass``.
    """
    inline = os.getenv("HIVEFLOW_SPREADSHEET_LAB_BASIC_AUTH", "").strip()
    if inline:
        return _split(inline)

    secret_arn = os.getenv("HIVEFLOW_SPREADSHEET_LAB_AUTH_SECRET_ARN", "").strip()
    if not secret_arn:
        return None
    return _split(_fetch_secret(secret_arn))


def auth_required() -> bool:
    return _resolve_credentials() is not None


def verify_login(username: str, password: str) -> bool:
    credentials = _resolve_credentials()
    if credentials is None:
        return True
    expected_user, expected_password = credentials
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(
        password, expected_password
    )


def _sign(key: str, value: str) -> str:
    return hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_cookie_value(username: str) -> str:
    """Signed with the current secret's password, so rotating the secret
    invalidates every outstanding cookie without needing a session store."""
    credentials = _resolve_credentials()
    if credentials is None:
        return username
    _, expected_password = credentials
    return f"{username}.{_sign(expected_password, username)}"


def is_session_valid(cookie_value: str | None) -> bool:
    credentials = _resolve_credentials()
    if credentials is None:
        return True
    if not cookie_value or "." not in cookie_value:
        return False
    username, _, signature = cookie_value.partition(".")
    expected_user, expected_password = credentials
    if not hmac.compare_digest(username, expected_user):
        return False
    return hmac.compare_digest(signature, _sign(expected_password, username))
