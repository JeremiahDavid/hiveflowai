"""Login-flow-only pieces split out of ``portal.auth``.

``portal.auth`` is the reusable session-token core both the portal shell and
satellite agent apps (Spreadsheet Engine, DNA Engine) import. Actually signing
a user in — loading configured portal users, verifying a password against
Cognito or the env/secrets fallback, and minting the resulting session cookie
response — is shell-only: no satellite app owns login, they only validate the
cookie the shell already issued. Keeping that surface in its own module keeps
``portal.auth`` import-light for everything that isn't the login page itself.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from typing import Any

from werkzeug.wrappers import Response

from hiveflow.dna.web.portal.auth import (
    PortalSession,
    client_id_from_reporting_hostname,
    create_session_token,
    is_global_portal_admin,
    is_global_portal_client_id,
    list_configured_portal_client_ids,
    normalize_portal_client_id,
    set_session_cookie,
    validate_portal_client_id_format,
)


@dataclass(frozen=True)
class PortalUser:
    username: str
    client_id: str
    password: str = ""


class PortalClientAccessError(Exception):
    """User-facing portal client selection failure."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def authorize_portal_client_access(
    *,
    username: str,
    identity_client_id: str,
    requested_client_id: str,
    fixed_client_id: str = "",
    env_config: dict[str, Any] | None = None,
) -> str:
    """Validate the requested portal client id and return the session value."""
    normalized = normalize_portal_client_id(requested_client_id)
    identity = normalize_portal_client_id(identity_client_id)
    if not normalized:
        if is_global_portal_admin(username=username, client_id=identity):
            raise PortalClientAccessError("Enter your client portal id.")
        normalized = identity
    if not normalized:
        raise PortalClientAccessError("Enter your client portal id.")
    if not validate_portal_client_id_format(normalized):
        raise PortalClientAccessError(
            "Client portal id must start with a letter and use lowercase letters or numbers."
        )

    fixed = normalize_portal_client_id(fixed_client_id)
    if fixed and normalized != fixed:
        raise PortalClientAccessError(f"This sign-in page is for the {fixed} portal.")

    if is_global_portal_admin(username=username, client_id=identity):
        configured = list_configured_portal_client_ids(env_config or {})
        if configured and normalized not in configured:
            raise PortalClientAccessError(f"Unknown client portal {normalized!r}.")
        return normalized

    if normalized != identity:
        raise PortalClientAccessError("That client portal id does not match your account.")
    return normalized


def resolve_login_client_id_hint(
    *,
    query_client_id: str = "",
    next_path: str = "",
    fixed_client_id: str = "",
    locked: bool = False,
) -> tuple[str, bool]:
    """Return default client id field value and whether it is read-only."""
    fixed = normalize_portal_client_id(fixed_client_id)
    if fixed:
        return fixed, True

    query = normalize_portal_client_id(query_client_id)
    if query and not is_global_portal_client_id(query):
        return query, locked

    if next_path.startswith("http://") or next_path.startswith("https://"):
        derived = client_id_from_reporting_hostname(next_path)
        if derived:
            return derived, locked

    return "", False


def load_portal_users(*, company: str, environment: str) -> dict[str, PortalUser]:
    import json
    import os

    users: dict[str, PortalUser] = {}

    raw_users = os.getenv("HIVEFLOW_PORTAL_USERS", "").strip()
    if raw_users:
        payload = json.loads(raw_users)
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                username = str(item.get("username", "")).strip().lower()
                password = str(item.get("password", ""))
                client_id = str(item.get("client_id", username)).strip().lower()
                if username and password:
                    users[username] = PortalUser(username=username, password=password, client_id=client_id)

    username = os.getenv("HIVEFLOW_PORTAL_USERNAME", "").strip().lower()
    password = os.getenv("HIVEFLOW_PORTAL_PASSWORD", "")
    client_id = os.getenv("HIVEFLOW_PORTAL_CLIENT_ID", company).strip().lower() or company.lower()
    if username and password:
        users[username] = PortalUser(username=username, password=password, client_id=client_id)

    secrets_path = os.getenv("HIVEFLOW_PORTAL_SECRETS_PATH", "").strip()
    if not secrets_path:
        from hiveflow.project_config import PROJECT_ROOT

        candidate = PROJECT_ROOT / "secrets" / f"{company.lower()}-portal-{environment.lower()}.yaml"
        if candidate.is_file():
            secrets_path = str(candidate)

    if secrets_path:
        import yaml

        with open(secrets_path, encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if isinstance(payload, dict):
            portal_users = payload.get("portal_users", payload.get("PORTAL_USERS", []))
            if isinstance(portal_users, list):
                for item in portal_users:
                    if not isinstance(item, dict):
                        continue
                    entry_username = str(item.get("username", "")).strip().lower()
                    entry_password = str(item.get("password", ""))
                    entry_client = str(item.get("client_id", entry_username)).strip().lower()
                    if entry_username and entry_password:
                        users[entry_username] = PortalUser(
                            username=entry_username,
                            password=entry_password,
                            client_id=entry_client,
                        )

    secret_name = os.getenv("HIVEFLOW_PORTAL_SECRET_NAME", "").strip()
    if secret_name:
        import json as json_module

        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_name)
        raw_secret = response.get("SecretString", "")
        payload = json_module.loads(raw_secret) if raw_secret else {}
        if isinstance(payload, dict):
            portal_users = payload.get("portal_users", payload.get("PORTAL_USERS", []))
            if isinstance(portal_users, list):
                for item in portal_users:
                    if not isinstance(item, dict):
                        continue
                    entry_username = str(item.get("username", "")).strip().lower()
                    entry_password = str(item.get("password", ""))
                    entry_client = str(item.get("client_id", entry_username)).strip().lower()
                    if entry_username and entry_password:
                        users[entry_username] = PortalUser(
                            username=entry_username,
                            password=entry_password,
                            client_id=entry_client,
                        )

    return users


def authenticate(
    username: str,
    password: str,
    *,
    company: str,
    environment: str,
) -> PortalUser | None:
    from hiveflow.dna.web.portal.cognito import authenticate_with_cognito, cognito_configured

    if cognito_configured():
        result = authenticate_with_cognito(
            username,
            password,
            company=company,
            environment=environment,
        )
        if result is None or result.kind != "authenticated" or result.user is None:
            return None
        return result.user

    normalized = username.strip().lower()
    users = load_portal_users(company=company, environment=environment)
    user = users.get(normalized)
    if user is None:
        return None
    if not hmac.compare_digest(user.password, password):
        return None
    return PortalUser(username=user.username, client_id=user.client_id)


def login_response(
    user: PortalUser,
    *,
    company: str,
    environment: str,
    redirect_to: str,
    portal_client_id: str | None = None,
) -> Response:
    session_client_id = normalize_portal_client_id(portal_client_id or user.client_id)
    token = create_session_token(
        PortalSession(username=user.username, client_id=session_client_id, issued_at=int(time.time())),
        company=company,
        environment=environment,
    )
    response = Response(status=302, headers={"Location": redirect_to})
    set_session_cookie(response, token)
    return response
