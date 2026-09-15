"""HTTP Basic Auth for the Spreadsheet Lab sandbox site.

Deliberately the simplest option — one shared username/password checked
against a Secrets Manager value — appropriate for a design-validation sandbox
seen only by the build team. Swap for real per-user auth before this UI is
shown to anyone else (see docs/spreadsheet-lab.md).
"""

from __future__ import annotations

import base64
import hmac
import os
from functools import lru_cache

from fastapi import Request
from fastapi.responses import Response

_REALM = "Spreadsheet Lab"


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


def challenge_unless_authorized(request: Request) -> Response | None:
    """Return a 401 challenge response, or ``None`` to let the request through."""
    credentials = _resolve_credentials()
    if credentials is None:
        return None

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode("utf-8")
            given_user, _, given_password = decoded.partition(":")
        except Exception:  # noqa: BLE001 — malformed header falls through to 401
            given_user, given_password = "", ""
        expected_user, expected_password = credentials
        if hmac.compare_digest(given_user, expected_user) and hmac.compare_digest(
            given_password, expected_password
        ):
            return None

    return Response(status_code=401, headers={"WWW-Authenticate": f'Basic realm="{_REALM}"'})
