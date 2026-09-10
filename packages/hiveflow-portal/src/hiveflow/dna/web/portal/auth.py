from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from werkzeug.wrappers import Request, Response

from hiveflow.dna.web.cognito_core import is_allowed_admin_username

SESSION_COOKIE = "hiveflow_portal_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 12

_SESSION_SECRET_CACHE: str | None = None


def session_cookie_name() -> str:
    """Cookie name — admin mode uses a separate name to avoid clashing with portal."""
    override = os.getenv("HIVEFLOW_SESSION_COOKIE", "").strip()
    return override or SESSION_COOKIE


def _load_secret_from_arn(secret_arn: str) -> str:
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    return str(response.get("SecretString", "")).strip()


def _session_secret(company: str, environment: str) -> str:
    global _SESSION_SECRET_CACHE  # noqa: PLW0603 — Lambda container reuse
    if _SESSION_SECRET_CACHE:
        return _SESSION_SECRET_CACHE

    configured = os.getenv("HIVEFLOW_PORTAL_SESSION_SECRET", "").strip()
    if configured:
        _SESSION_SECRET_CACHE = configured
        return configured

    secret_arn = os.getenv("HIVEFLOW_PORTAL_SESSION_SECRET_ARN", "").strip()
    if secret_arn:
        _SESSION_SECRET_CACHE = _load_secret_from_arn(secret_arn)
        if _SESSION_SECRET_CACHE:
            return _SESSION_SECRET_CACHE

    seed = f"{company}:{environment}:hiveflow-portal"
    _SESSION_SECRET_CACHE = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return _SESSION_SECRET_CACHE


@dataclass(frozen=True)
class PortalUser:
    username: str
    client_id: str
    password: str = ""


@dataclass(frozen=True)
class PortalSession:
    username: str
    client_id: str
    issued_at: int


def global_portal_client_id() -> str:
    return os.getenv("HIVEFLOW_GLOBAL_PORTAL_CLIENT_ID", "platform").strip().lower() or "platform"


def is_global_portal_client_id(client_id: str) -> bool:
    return client_id.strip().lower() == global_portal_client_id()


def is_global_portal_admin(*, username: str, client_id: str) -> bool:
    """Platform operators (GlobalAdmin) may access any client reporting portal."""
    if not is_global_portal_client_id(client_id):
        return False
    return is_allowed_admin_username(username)


def effective_portal_client_id(session: PortalSession, *, fixed_client_id: str = "") -> str:
    """Return the portal client id stored in the session (chosen at login)."""
    _ = fixed_client_id
    return session.client_id


def normalize_portal_client_id(raw: str) -> str:
    return raw.strip().lower()


def list_configured_portal_client_ids(env_config: dict[str, Any]) -> set[str]:
    ui_cfg = env_config.get("ui", {})
    if not isinstance(ui_cfg, dict):
        ui_cfg = {}
    portal_cfg = ui_cfg.get("portal", {})
    if not isinstance(portal_cfg, dict):
        portal_cfg = {}
    clients = portal_cfg.get("clients", {})
    if not isinstance(clients, dict):
        return set()
    return {normalize_portal_client_id(str(key)) for key in clients if normalize_portal_client_id(str(key))}


def validate_portal_client_id_format(client_id: str) -> bool:
    from hiveflow.client_registry import CLIENT_ID_RE

    normalized = normalize_portal_client_id(client_id)
    return bool(normalized) and bool(CLIENT_ID_RE.match(normalized))


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


def client_id_from_reporting_hostname(url: str) -> str | None:
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip().lstrip(".")
    if not cookie_domain:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname or hostname == cookie_domain:
        return None
    suffix = f".{cookie_domain}"
    if not hostname.endswith(suffix):
        return None
    subdomain = hostname[: -len(suffix)].strip().lower()
    return subdomain or None


def _sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(session: PortalSession, *, company: str, environment: str) -> str:
    body = json.dumps(
        {
            "username": session.username,
            "client_id": session.client_id,
            "issued_at": session.issued_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    signature = _sign_payload(body, _session_secret(company, environment))
    return f"{body}.{signature}"


def read_session_token(token: str, *, company: str, environment: str) -> PortalSession | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = _sign_payload(body, _session_secret(company, environment))
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    issued_at = int(payload.get("issued_at", 0))
    if issued_at <= 0 or time.time() - issued_at > SESSION_MAX_AGE_SECONDS:
        return None

    username = str(payload.get("username", "")).strip()
    client_id = str(payload.get("client_id", "")).strip()
    if not username or not client_id:
        return None
    return PortalSession(username=username, client_id=client_id, issued_at=issued_at)


def session_from_request(request: Request, *, company: str, environment: str) -> PortalSession | None:
    token = request.cookies.get(session_cookie_name(), "")
    return read_session_token(token, company=company, environment=environment)


def set_session_cookie(response: Response, token: str) -> None:
    cookie_kwargs: dict[str, Any] = {
        "max_age": SESSION_MAX_AGE_SECONDS,
        "httponly": True,
        "samesite": "Lax",
        "secure": os.getenv("HIVEFLOW_PORTAL_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"},
        "path": "/",
    }
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip()
    if cookie_domain:
        cookie_kwargs["domain"] = cookie_domain
    response.set_cookie(session_cookie_name(), token, **cookie_kwargs)


def clear_session_cookie(response: Response) -> None:
    name = session_cookie_name()
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip()
    if cookie_domain:
        response.delete_cookie(name, path="/", domain=cookie_domain)
    else:
        response.delete_cookie(name, path="/")
    # Also drop a colliding portal cookie on shared parent domains (admin host-only mode).
    if name != SESSION_COOKIE and not cookie_domain:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(SESSION_COOKIE, path="/", domain=".hive-flow-ai.com")


def load_portal_users(*, company: str, environment: str) -> dict[str, PortalUser]:
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


def require_portal_session(
    request: Request,
    *,
    company: str,
    environment: str,
    login_url: str,
) -> tuple[PortalSession | None, Response | None]:
    session = session_from_request(request, company=company, environment=environment)
    if session is None:
        next_path = request.full_path if request.query_string else request.path
        location = f"{login_url}?next={next_path}" if next_path and next_path != "?" else login_url
        return None, Response(status=302, headers={"Location": location})
    return session, None


def require_portal_admin(
    username: str,
    *,
    company: str,
    environment: str,
) -> bool:
    from hiveflow.dna.web.portal.cognito import portal_user_is_admin

    return portal_user_is_admin(username, company=company, environment=environment)


# ── Starlette / FastAPI edges ────────────────────────────────────────────────
#
# The functions above take a ``werkzeug`` Request/Response. During the
# incremental FastAPI migration native routes need Starlette equivalents.
# ``session_from_request`` / ``set_session_cookie`` / ``clear_session_cookie``
# already duck-type across both frameworks (``.cookies`` dict, matching
# ``set_cookie`` / ``delete_cookie`` kwargs), so only the request-introspecting
# ``require_portal_session`` needs a variant.


def _starlette_next_path(request: Any) -> str:
    """``path[?query]`` for a Starlette request (mirrors werkzeug ``full_path``)."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def require_portal_session_starlette(
    request: Any,
    *,
    company: str,
    environment: str,
    login_url: str,
):
    """Return ``(session, redirect_response_or_None)`` for a Starlette request."""
    from starlette.responses import RedirectResponse

    session = session_from_request(request, company=company, environment=environment)
    if session is not None:
        return session, None
    next_path = _starlette_next_path(request)
    location = f"{login_url}?next={next_path}" if next_path and next_path != "?" else login_url
    return None, RedirectResponse(location, status_code=302)
