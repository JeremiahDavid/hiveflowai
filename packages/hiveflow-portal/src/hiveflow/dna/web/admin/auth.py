"""Platform admin Cognito helpers (separate pool from client portal)."""

from __future__ import annotations

import os
from typing import Any

from hiveflow.dna.web.cognito_core import (
    ADMIN_GROUP_NAME,
    admin_get_user as _admin_get_user,
    admin_username_allowlist,
    attribute_map as _attribute_map,
    build_user_attributes as _build_user_attributes,
    cognito_client as _cognito_client,
    is_admin_group_member,
    is_allowed_admin_username,
)
from hiveflow.dna.web.portal.auth import global_portal_client_id
from hiveflow.dna.web.portal.auth_login import PortalUser
from hiveflow.dna.web.portal.cognito import (
    NEW_PASSWORD_CHALLENGE,
    PORTAL_ROLE_ADMIN,
    CognitoConfig,
    NewPasswordChallenge,
    PortalLoginResult,
    PortalUserAlreadyExists,
    authenticate_with_cognito,
    complete_new_password_challenge,
    find_user_by_email,
    load_cognito_config,
)


def is_platform_admin(username: str, *, company: str, environment: str) -> bool:
    """Admin-panel access: Cognito group membership in the admin pool."""
    config = load_cognito_config(company=company, environment=environment)
    if config is None:
        return False
    return is_admin_group_member(username, user_pool_id=config.user_pool_id, region=config.region)


def authenticate_admin(
    username: str,
    password: str,
    *,
    company: str,
    environment: str,
) -> PortalLoginResult | None:
    """Authenticate against the admin Cognito pool; reject non-allowlisted usernames."""
    result = authenticate_with_cognito(
        username,
        password,
        company=company,
        environment=environment,
    )
    if result is None:
        return None
    if result.kind == "new_password" and result.challenge is not None:
        # Allow challenge to proceed; final username is checked after password set.
        # If the typed identifier was email, Cognito still returns the pool username later.
        return result
    if result.kind == "authenticated" and result.user is not None:
        if not is_platform_admin(result.user.username, company=company, environment=environment):
            return None
        return PortalLoginResult(
            kind="authenticated",
            user=PortalUser(username=result.user.username, client_id="platform"),
        )
    return None


def complete_admin_new_password(
    *,
    username: str,
    session: str,
    new_password: str,
    company: str,
    environment: str,
) -> PortalUser | None:
    user = complete_new_password_challenge(
        username=username,
        session=session,
        new_password=new_password,
        company=company,
        environment=environment,
    )
    if user is None or not is_platform_admin(user.username, company=company, environment=environment):
        return None
    return PortalUser(username=user.username, client_id="platform")


def _user_email(user_payload: dict[str, Any]) -> str:
    attrs = _attribute_map(user_payload.get("UserAttributes"))
    return str(attrs.get("email") or "").strip()


def _apply_permanent_password(
    client: Any,
    *,
    user_pool_id: str,
    username: str,
    password: str,
) -> None:
    client.admin_set_user_password(
        UserPoolId=user_pool_id.strip(),
        Username=username.strip(),
        Password=password,
        Permanent=True,
    )


def _update_portal_global_admin(
    client: Any,
    *,
    portal_user_pool_id: str,
    username: str,
    email: str,
    user_status: str = "UNKNOWN",
    temporary_password: str | None = None,
) -> dict[str, Any]:
    platform_client_id = global_portal_client_id()
    canonical = username.strip()
    attributes = _build_user_attributes(
        client_id=platform_client_id,
        email=email,
        role=PORTAL_ROLE_ADMIN,
    )
    client.admin_update_user_attributes(
        UserPoolId=portal_user_pool_id.strip(),
        Username=canonical,
        UserAttributes=attributes,
    )
    if temporary_password:
        _apply_permanent_password(
            client,
            user_pool_id=portal_user_pool_id,
            username=canonical,
            password=temporary_password,
        )
    return {
        "portal_status": "updated",
        "portal_username": canonical,
        "portal_client_id": platform_client_id,
        "user_status": user_status,
    }


def _ensure_portal_global_admin(
    client: Any,
    *,
    portal_user_pool_id: str,
    admin_username: str,
    email: str,
    temporary_password: str | None = None,
) -> dict[str, Any]:
    """Ensure GlobalAdmin exists in the shared portal pool with cross-client access."""
    platform_client_id = global_portal_client_id()
    normalized_username = admin_username.strip()
    pool_id = portal_user_pool_id.strip()
    email_owner = find_user_by_email(client, user_pool_id=pool_id, email=email)
    portal_email = email.strip()
    if email_owner is not None and email_owner.casefold() != normalized_username.casefold():
        portal_email = ""
    attributes = _build_user_attributes(
        client_id=platform_client_id,
        email=portal_email,
        role=PORTAL_ROLE_ADMIN,
    )

    existing = _admin_get_user(
        client,
        user_pool_id=pool_id,
        username=normalized_username,
    )
    if existing is not None:
        canonical = str(existing.get("Username", normalized_username)).strip() or normalized_username
        return _update_portal_global_admin(
            client,
            portal_user_pool_id=pool_id,
            username=canonical,
            email=portal_email,
            user_status=str(existing.get("UserStatus", "UNKNOWN")),
            temporary_password=temporary_password,
        )

    email_note = ""
    if email.strip() and not portal_email:
        email_note = (
            f"Portal GlobalAdmin created without email because {email!r} is already assigned "
            f"to portal user {email_owner!r}. Sign in with username {normalized_username!r}."
        )

    create_kwargs: dict[str, Any] = {
        "UserPoolId": pool_id,
        "Username": normalized_username,
        "UserAttributes": attributes,
        "MessageAction": "SUPPRESS",
    }
    if temporary_password:
        create_kwargs["TemporaryPassword"] = temporary_password
    try:
        response = client.admin_create_user(**create_kwargs)
    except client.exceptions.UsernameExistsException:
        existing = _admin_get_user(
            client,
            user_pool_id=pool_id,
            username=normalized_username,
        )
        if existing is not None:
            canonical = str(existing.get("Username", normalized_username)).strip() or normalized_username
            return _update_portal_global_admin(
                client,
                portal_user_pool_id=pool_id,
                username=canonical,
                email=portal_email,
                user_status=str(existing.get("UserStatus", "UNKNOWN")),
                temporary_password=temporary_password,
            )
        raise PortalUserAlreadyExists(
            f"User {normalized_username!r} already exists in portal pool {pool_id!r}."
        ) from None

    canonical = str(response.get("User", {}).get("Username", normalized_username)).strip() or normalized_username
    if temporary_password:
        _apply_permanent_password(
            client,
            user_pool_id=pool_id,
            username=canonical,
            password=temporary_password,
        )
    result = {
        "portal_status": "created",
        "portal_username": canonical,
        "portal_client_id": platform_client_id,
        "user_status": response.get("User", {}).get("UserStatus", "UNKNOWN"),
    }
    if email_note:
        result["portal_email_note"] = email_note
    return result


def bootstrap_global_admin(
    *,
    portal_user_pool_id: str,
    admin_user_pool_id: str,
    portal_username: str = "AdminPOC",
    admin_username: str | None = None,
    region: str | None = None,
    temporary_password: str | None = None,
) -> dict[str, Any]:
    """Copy AdminPOC email from the portal pool and create GlobalAdmin in both Cognito pools."""
    region_name = (
        (region or "").strip()
        or os.getenv("AWS_REGION", "").strip()
        or os.getenv("AWS_DEFAULT_REGION", "").strip()
        or "us-east-2"
    )
    target_username = (admin_username or admin_username_allowlist()).strip()
    client = _cognito_client(region_name)

    portal_user = _admin_get_user(
        client,
        user_pool_id=portal_user_pool_id.strip(),
        username=portal_username.strip(),
    )
    if portal_user is None:
        raise RuntimeError(f"Portal user {portal_username!r} not found in {portal_user_pool_id}")

    email = _user_email(portal_user)
    if not email:
        raise RuntimeError(f"Portal user {portal_username!r} has no email attribute")

    portal_global_admin = _ensure_portal_global_admin(
        client,
        portal_user_pool_id=portal_user_pool_id,
        admin_username=target_username,
        email=email,
        temporary_password=temporary_password,
    )

    existing = _admin_get_user(
        client,
        user_pool_id=admin_user_pool_id.strip(),
        username=target_username,
    )
    if existing is not None:
        if temporary_password:
            _apply_permanent_password(
                client,
                user_pool_id=admin_user_pool_id.strip(),
                username=target_username,
                password=temporary_password,
            )
        client.admin_add_user_to_group(
            UserPoolId=admin_user_pool_id.strip(),
            Username=target_username,
            GroupName=ADMIN_GROUP_NAME,
        )
        return {
            "status": "exists",
            "username": target_username,
            "email": email,
            "admin_user_pool_id": admin_user_pool_id,
            "source_portal_user": portal_username,
            **portal_global_admin,
        }

    create_kwargs: dict[str, Any] = {
        "UserPoolId": admin_user_pool_id.strip(),
        "Username": target_username,
        "UserAttributes": [
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
    }
    if temporary_password:
        create_kwargs["TemporaryPassword"] = temporary_password
        create_kwargs["MessageAction"] = "SUPPRESS"
    try:
        response = client.admin_create_user(**create_kwargs)
    except client.exceptions.UsernameExistsException:
        existing = _admin_get_user(
            client,
            user_pool_id=admin_user_pool_id.strip(),
            username=target_username,
        )
        if existing is None:
            raise PortalUserAlreadyExists(f"User {target_username!r} already exists.") from None
        if temporary_password:
            _apply_permanent_password(
                client,
                user_pool_id=admin_user_pool_id.strip(),
                username=target_username,
                password=temporary_password,
            )
        client.admin_add_user_to_group(
            UserPoolId=admin_user_pool_id.strip(),
            Username=target_username,
            GroupName=ADMIN_GROUP_NAME,
        )
        return {
            "status": "exists",
            "username": target_username,
            "email": email,
            "admin_user_pool_id": admin_user_pool_id,
            "source_portal_user": portal_username,
            **portal_global_admin,
        }
    if temporary_password:
        _apply_permanent_password(
            client,
            user_pool_id=admin_user_pool_id.strip(),
            username=target_username,
            password=temporary_password,
        )
    client.admin_add_user_to_group(
        UserPoolId=admin_user_pool_id.strip(),
        Username=target_username,
        GroupName=ADMIN_GROUP_NAME,
    )

    return {
        "status": "created",
        "username": target_username,
        "email": email,
        "admin_user_pool_id": admin_user_pool_id,
        "source_portal_user": portal_username,
        "user_status": response.get("User", {}).get("UserStatus", "UNKNOWN"),
        "delivery": "invite_email" if not temporary_password else "temporary_suppressed",
        **portal_global_admin,
    }


# Re-export for typing convenience in views/tests
__all__ = [
    "CognitoConfig",
    "NewPasswordChallenge",
    "NEW_PASSWORD_CHALLENGE",
    "PortalLoginResult",
    "admin_username_allowlist",
    "authenticate_admin",
    "bootstrap_global_admin",
    "complete_admin_new_password",
    "is_allowed_admin_username",
    "is_platform_admin",
    "load_cognito_config",
]
