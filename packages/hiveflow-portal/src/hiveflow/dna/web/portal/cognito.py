"""Amazon Cognito authentication for the HiveFlowAI client portal."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from hiveflow.dna.web.cognito_core import (
    admin_get_user as _admin_get_user,
    attribute_map as _attribute_map,
    build_user_attributes as _build_user_attributes,
    cognito_client as _cognito_client,
)
from hiveflow.dna.web.portal.auth_login import PortalUser

logger = logging.getLogger("hiveflow.portal.cognito")

CLIENT_ID_ATTRIBUTE = "custom:client_id"
ROLE_ATTRIBUTE = "custom:portal_role"
PORTAL_ROLE_ADMIN = "admin"
PORTAL_ROLE_MEMBER = "member"
NEW_PASSWORD_CHALLENGE = "NEW_PASSWORD_REQUIRED"


class PortalUserLimitExceeded(Exception):
    def __init__(self, max_users: int) -> None:
        self.max_users = max_users
        super().__init__(f"Portal seat limit reached ({max_users} users).")


class PortalUserAlreadyExists(Exception):
    pass


@dataclass(frozen=True)
class CognitoConfig:
    user_pool_id: str
    client_id: str
    region: str
    default_client_id: str


@dataclass(frozen=True)
class NewPasswordChallenge:
    username: str
    session: str


@dataclass(frozen=True)
class PortalLoginResult:
    kind: Literal["authenticated", "new_password"]
    user: PortalUser | None = None
    challenge: NewPasswordChallenge | None = None


@dataclass(frozen=True)
class PortalUserRecord:
    username: str
    email: str
    client_id: str
    role: str
    status: str
    enabled: bool


def cognito_configured() -> bool:
    return bool(os.getenv("HIVEFLOW_COGNITO_USER_POOL_ID", "").strip())


def load_cognito_config(*, company: str, environment: str) -> CognitoConfig | None:
    user_pool_id = os.getenv("HIVEFLOW_COGNITO_USER_POOL_ID", "").strip()
    client_id = os.getenv("HIVEFLOW_COGNITO_CLIENT_ID", "").strip()
    if not user_pool_id or not client_id:
        return None
    region = (
        os.getenv("HIVEFLOW_COGNITO_REGION", "").strip()
        or os.getenv("AWS_REGION", "").strip()
        or os.getenv("AWS_DEFAULT_REGION", "").strip()
        or "us-east-2"
    )
    default_client_id = (
        os.getenv("HIVEFLOW_PORTAL_DEFAULT_CLIENT_ID", "").strip().lower()
        or company.strip().lower()
        or "default"
    )
    return CognitoConfig(
        user_pool_id=user_pool_id,
        client_id=client_id,
        region=region,
        default_client_id=default_client_id,
    )


def find_user_by_email(
    client: Any,
    *,
    user_pool_id: str,
    email: str,
) -> str | None:
    """Return the Cognito username that owns ``email``, if any."""
    needle = email.strip().casefold()
    if not needle:
        return None
    paginator = client.get_paginator("list_users")
    for page in paginator.paginate(UserPoolId=user_pool_id):
        for entry in page.get("Users", []):
            attributes = _attribute_map(entry.get("Attributes"))
            if attributes.get("email", "").strip().casefold() == needle:
                candidate = str(entry.get("Username", "")).strip()
                if candidate:
                    return candidate
    return None


def resolve_client_id(
    attributes: dict[str, str],
    *,
    default_client_id: str,
    username: str,
) -> str:
    client_id = attributes.get(CLIENT_ID_ATTRIBUTE, "").strip().lower()
    if client_id:
        return client_id
    fallback = default_client_id.strip().lower() or username.strip().lower()
    return fallback or "default"


def resolve_portal_role(attributes: dict[str, str]) -> str:
    role = attributes.get(ROLE_ATTRIBUTE, "").strip().lower()
    if role in (PORTAL_ROLE_ADMIN, PORTAL_ROLE_MEMBER):
        return role
    # Default is least-privilege. Real admins carry an explicit
    # ``custom:portal_role=admin`` (backfilled by scripts/backfill_portal_roles.py);
    # GlobalAdmin is recognised separately by username.
    return PORTAL_ROLE_MEMBER


def _user_record_from_cognito(entry: dict[str, Any], *, default_client_id: str) -> PortalUserRecord | None:
    # Preserve Cognito username casing — pools are case-sensitive by default.
    username = str(entry.get("Username", "")).strip()
    if not username:
        return None
    attributes = _attribute_map(entry.get("Attributes"))
    client_id = resolve_client_id(
        attributes,
        default_client_id=default_client_id,
        username=username,
    )
    return PortalUserRecord(
        username=username,
        email=attributes.get("email", ""),
        client_id=client_id,
        role=resolve_portal_role(attributes),
        status=str(entry.get("UserStatus", "UNKNOWN")),
        enabled=bool(entry.get("Enabled", True)),
    )


def _list_users_for_client_filter(
    client: Any,
    *,
    config: CognitoConfig,
    client_id: str,
) -> list[PortalUserRecord]:
    normalized_client = client_id.strip().lower()
    users: list[PortalUserRecord] = []
    paginator = client.get_paginator("list_users")
    # Cognito ListUsers filters only support standard attributes — scan and filter client-side.
    for page in paginator.paginate(UserPoolId=config.user_pool_id):
        for entry in page.get("Users", []):
            record = _user_record_from_cognito(entry, default_client_id=config.default_client_id)
            if record is not None and record.client_id == normalized_client:
                users.append(record)
    users.sort(key=lambda item: item.username)
    return users


def count_portal_users_for_client(
    *,
    client_id: str,
    company: str,
    environment: str,
) -> int:
    return len(list_portal_users_for_client(client_id=client_id, company=company, environment=environment))


def list_portal_users_for_client(
    *,
    client_id: str,
    company: str,
    environment: str,
) -> list[PortalUserRecord]:
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        return []

    client = _cognito_client(config.region)
    return _list_users_for_client_filter(client, config=config, client_id=client_id)


def set_portal_user_role(
    *,
    username: str,
    role: str,
    company: str,
    environment: str,
) -> dict[str, Any]:
    """Update ``custom:portal_role`` for an existing Cognito portal user."""
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        raise RuntimeError(
            "Cognito is not configured. Set HIVEFLOW_COGNITO_USER_POOL_ID and HIVEFLOW_COGNITO_CLIENT_ID."
        )
    normalized_role = role.strip().lower()
    if normalized_role not in (PORTAL_ROLE_ADMIN, PORTAL_ROLE_MEMBER):
        raise ValueError(f"role must be {PORTAL_ROLE_ADMIN!r} or {PORTAL_ROLE_MEMBER!r}")
    normalized = username.strip()
    if not normalized:
        raise ValueError("username is required")

    client = _cognito_client(config.region)
    user_response = _admin_get_user(
        client,
        user_pool_id=config.user_pool_id,
        username=normalized,
    )
    if user_response is None:
        raise ValueError(f"User {normalized!r} was not found.")
    canonical = str(user_response.get("Username", normalized)).strip() or normalized
    client.admin_update_user_attributes(
        UserPoolId=config.user_pool_id,
        Username=canonical,
        UserAttributes=[{"Name": ROLE_ATTRIBUTE, "Value": normalized_role}],
    )
    return {"username": canonical, "role": normalized_role}


def portal_user_is_admin(
    username: str,
    *,
    company: str,
    environment: str,
) -> bool:
    from hiveflow.dna.web.portal.auth import is_global_portal_admin

    if not cognito_configured():
        return True

    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        return False

    normalized = username.strip()
    if not normalized:
        return False

    try:
        client = _cognito_client(config.region)
        user_response = _admin_get_user(
            client,
            user_pool_id=config.user_pool_id,
            username=normalized,
        )
    except Exception:  # noqa: BLE001 — treat Cognito lookup failures as non-admin
        return False
    if user_response is None:
        return False

    attributes = _attribute_map(user_response.get("UserAttributes"))
    cognito_client_id = resolve_client_id(
        attributes,
        default_client_id=config.default_client_id,
        username=normalized,
    )
    if is_global_portal_admin(username=normalized, client_id=cognito_client_id):
        return True
    return resolve_portal_role(attributes) == PORTAL_ROLE_ADMIN


def _portal_user_from_username(
    client: Any,
    *,
    config: CognitoConfig,
    username: str,
) -> PortalUser:
    user_response = _admin_get_user(
        client,
        user_pool_id=config.user_pool_id,
        username=username,
    )
    if user_response is None:
        raise ValueError(f"User {username!r} was not found.")
    attributes = _attribute_map(user_response.get("UserAttributes"))
    # Keep Cognito's exact Username — lowercasing breaks AdminGetUser on case-sensitive pools.
    login_name = str(user_response.get("Username", username)).strip() or username.strip()
    client_id = resolve_client_id(
        attributes,
        default_client_id=config.default_client_id,
        username=login_name,
    )
    return PortalUser(username=login_name, client_id=client_id)


def authenticate_with_cognito(
    username: str,
    password: str,
    *,
    company: str,
    environment: str,
) -> PortalLoginResult | None:
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        logger.warning(
            "cognito_auth_abort reason=no_config pool_id_set=%s client_id_set=%s",
            bool(os.getenv("HIVEFLOW_COGNITO_USER_POOL_ID", "").strip()),
            bool(os.getenv("HIVEFLOW_COGNITO_CLIENT_ID", "").strip()),
        )
        return None

    normalized = username.strip()
    if not normalized or not password:
        logger.warning(
            "cognito_auth_abort reason=empty_field username_len=%d password_len=%d",
            len(normalized),
            len(password),
        )
        return None

    client = _cognito_client(config.region)
    try:
        auth_response = client.admin_initiate_auth(
            UserPoolId=config.user_pool_id,
            ClientId=config.client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": normalized,
                "PASSWORD": password,
            },
        )
    except client.exceptions.NotAuthorizedException as exc:
        logger.warning(
            "cognito_auth_failed reason=not_authorized username=%r pool=%s client=%s detail=%s",
            normalized,
            config.user_pool_id,
            config.client_id,
            exc,
        )
        return None
    except client.exceptions.UserNotFoundException as exc:
        logger.warning(
            "cognito_auth_failed reason=user_not_found username=%r pool=%s detail=%s",
            normalized,
            config.user_pool_id,
            exc,
        )
        return None
    except Exception:  # noqa: BLE001 — log full context before it becomes a 500
        logger.exception(
            "cognito_auth_unexpected_error username=%r pool=%s client=%s",
            normalized,
            config.user_pool_id,
            config.client_id,
        )
        raise

    challenge = auth_response.get("ChallengeName")
    if challenge == NEW_PASSWORD_CHALLENGE:
        session = str(auth_response.get("Session", "")).strip()
        if not session:
            logger.warning("cognito_auth_abort reason=new_password_no_session username=%r", normalized)
            return None
        return PortalLoginResult(
            kind="new_password",
            challenge=NewPasswordChallenge(username=normalized, session=session),
        )
    if challenge:
        logger.warning("cognito_auth_abort reason=unhandled_challenge challenge=%s username=%r", challenge, normalized)
        return None

    user = _portal_user_from_username(client, config=config, username=normalized)
    return PortalLoginResult(kind="authenticated", user=user)


def complete_new_password_challenge(
    *,
    username: str,
    session: str,
    new_password: str,
    company: str,
    environment: str,
) -> PortalUser | None:
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        return None

    normalized = username.strip()
    if not normalized or not session.strip() or not new_password:
        return None

    client = _cognito_client(config.region)
    try:
        response = client.admin_respond_to_auth_challenge(
            UserPoolId=config.user_pool_id,
            ClientId=config.client_id,
            ChallengeName=NEW_PASSWORD_CHALLENGE,
            Session=session.strip(),
            ChallengeResponses={
                "USERNAME": normalized,
                "NEW_PASSWORD": new_password,
            },
        )
    except client.exceptions.InvalidPasswordException:
        return None
    except client.exceptions.NotAuthorizedException:
        return None

    if response.get("ChallengeName"):
        return None

    return _portal_user_from_username(client, config=config, username=normalized)


class PasswordResetError(Exception):
    """User-facing password reset failure with a safe public message."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def request_password_reset(
    username: str,
    *,
    company: str,
    environment: str,
) -> None:
    """Send a Cognito forgot-password code to the user's verified email.

    Always succeeds from the caller's perspective unless Cognito is unavailable
    or rate-limited — unknown usernames are treated as success so we do not
    leak account existence.
    """
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        raise PasswordResetError("Password reset is not available right now.")

    normalized = username.strip()
    if not normalized:
        raise PasswordResetError("Enter your username or email.")

    client = _cognito_client(config.region)
    try:
        client.forgot_password(
            ClientId=config.client_id,
            Username=normalized,
        )
    except client.exceptions.UserNotFoundException:
        return
    except client.exceptions.InvalidParameterException:
        # Common when the user has no verified email on file.
        return
    except client.exceptions.LimitExceededException as exc:
        raise PasswordResetError(
            "Too many reset attempts. Please wait a few minutes and try again."
        ) from exc
    except client.exceptions.NotAuthorizedException:
        return
    except Exception as exc:  # noqa: BLE001 — map unexpected Cognito errors
        raise PasswordResetError("Could not start password reset. Please try again.") from exc


def confirm_password_reset(
    *,
    username: str,
    confirmation_code: str,
    new_password: str,
    company: str,
    environment: str,
) -> None:
    """Confirm a forgot-password code and set a new permanent password."""
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        raise PasswordResetError("Password reset is not available right now.")

    normalized = username.strip()
    code = confirmation_code.strip()
    if not normalized or not code or not new_password:
        raise PasswordResetError("Username, code, and new password are required.")

    client = _cognito_client(config.region)
    try:
        client.confirm_forgot_password(
            ClientId=config.client_id,
            Username=normalized,
            ConfirmationCode=code,
            Password=new_password,
        )
    except client.exceptions.CodeMismatchException as exc:
        raise PasswordResetError("That reset code is incorrect.") from exc
    except client.exceptions.ExpiredCodeException as exc:
        raise PasswordResetError("That reset code has expired. Request a new one.") from exc
    except client.exceptions.InvalidPasswordException as exc:
        raise PasswordResetError(
            "Password does not meet requirements (min 12 characters, upper, lower, and a number)."
        ) from exc
    except client.exceptions.UserNotFoundException as exc:
        raise PasswordResetError("Could not reset that password. Check the username and try again.") from exc
    except client.exceptions.LimitExceededException as exc:
        raise PasswordResetError(
            "Too many reset attempts. Please wait a few minutes and try again."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — map unexpected Cognito errors
        raise PasswordResetError("Could not reset your password. Please try again.") from exc


def _enforce_user_limit_after_create(
    client: Any,
    *,
    config: CognitoConfig,
    username: str,
    client_id: str,
    max_users: int | None,
) -> None:
    if max_users is None:
        return
    count = len(_list_users_for_client_filter(client, config=config, client_id=client_id))
    if count > max_users:
        client.admin_delete_user(UserPoolId=config.user_pool_id, Username=username)
        raise PortalUserLimitExceeded(max_users)


def create_portal_user(
    *,
    username: str,
    password: str | None,
    client_id: str,
    email: str = "",
    company: str,
    environment: str,
    permanent_password: bool = True,
    role: str = PORTAL_ROLE_ADMIN,
    max_users: int | None = None,
    enforce_limit: bool = False,
) -> dict[str, Any]:
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        raise RuntimeError(
            "Cognito is not configured. Set HIVEFLOW_COGNITO_USER_POOL_ID and HIVEFLOW_COGNITO_CLIENT_ID."
        )

    normalized = username.strip()
    if not normalized:
        raise ValueError("username is required")

    normalized_client = client_id.strip().lower()
    if enforce_limit and max_users is not None:
        current = count_portal_users_for_client(
            client_id=normalized_client,
            company=company,
            environment=environment,
        )
        if current >= max_users:
            raise PortalUserLimitExceeded(max_users)

    attributes = _build_user_attributes(client_id=normalized_client, email=email, role=role)

    client = _cognito_client(config.region)
    create_kwargs: dict[str, Any] = {
        "UserPoolId": config.user_pool_id,
        "Username": normalized,
        "UserAttributes": attributes,
        "MessageAction": "SUPPRESS",
    }
    if password:
        create_kwargs["TemporaryPassword"] = password
    try:
        response = client.admin_create_user(**create_kwargs)
    except client.exceptions.UsernameExistsException as exc:
        raise PortalUserAlreadyExists(f"User {normalized!r} already exists.") from exc

    if password and permanent_password:
        client.admin_set_user_password(
            UserPoolId=config.user_pool_id,
            Username=normalized,
            Password=password,
            Permanent=True,
        )

    _enforce_user_limit_after_create(
        client,
        config=config,
        username=normalized,
        client_id=normalized_client,
        max_users=max_users if enforce_limit else None,
    )

    return {
        "username": normalized,
        "client_id": normalized_client,
        "role": role,
        "status": response.get("User", {}).get("UserStatus", "UNKNOWN"),
        "delivery": "permanent",
    }


def invite_portal_user(
    *,
    username: str,
    client_id: str,
    email: str,
    company: str,
    environment: str,
    temporary_password: str | None = None,
    max_users: int | None = None,
    enforce_limit: bool = True,
    role: str = PORTAL_ROLE_MEMBER,
) -> dict[str, Any]:
    """Create a user and email a temporary password via Cognito."""
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        raise RuntimeError(
            "Cognito is not configured. Set HIVEFLOW_COGNITO_USER_POOL_ID and HIVEFLOW_COGNITO_CLIENT_ID."
        )

    normalized = username.strip()
    email_value = email.strip()
    if not normalized:
        raise ValueError("username is required")
    if not email_value:
        raise ValueError("email is required for invite delivery")

    normalized_role = role.strip().lower() or PORTAL_ROLE_MEMBER
    if normalized_role not in (PORTAL_ROLE_ADMIN, PORTAL_ROLE_MEMBER):
        raise ValueError(f"role must be {PORTAL_ROLE_ADMIN!r} or {PORTAL_ROLE_MEMBER!r}")

    normalized_client = client_id.strip().lower()
    if enforce_limit and max_users is not None:
        current = count_portal_users_for_client(
            client_id=normalized_client,
            company=company,
            environment=environment,
        )
        if current >= max_users:
            raise PortalUserLimitExceeded(max_users)

    attributes = _build_user_attributes(
        client_id=normalized_client,
        email=email_value,
        role=normalized_role,
    )

    client = _cognito_client(config.region)
    create_kwargs: dict[str, Any] = {
        "UserPoolId": config.user_pool_id,
        "Username": normalized,
        "UserAttributes": attributes,
        "DesiredDeliveryMediums": ["EMAIL"],
    }
    if temporary_password:
        create_kwargs["TemporaryPassword"] = temporary_password

    try:
        response = client.admin_create_user(**create_kwargs)
    except client.exceptions.UsernameExistsException as exc:
        raise PortalUserAlreadyExists(f"User {normalized!r} already exists.") from exc

    _enforce_user_limit_after_create(
        client,
        config=config,
        username=normalized,
        client_id=normalized_client,
        max_users=max_users if enforce_limit else None,
    )

    return {
        "username": normalized,
        "client_id": normalized_client,
        "email": email_value,
        "role": normalized_role,
        "status": response.get("User", {}).get("UserStatus", "UNKNOWN"),
        "delivery": "invite_email",
    }
