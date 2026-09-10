"""Tests for Cognito-backed portal authentication."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from werkzeug.test import Client

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import create_app
from hiveflow.dna.web.portal.auth import authenticate
from hiveflow.dna.web.portal.cognito import (
    CLIENT_ID_ATTRIBUTE,
    ROLE_ATTRIBUTE,
    NewPasswordChallenge,
    PasswordResetError,
    PortalLoginResult,
    PortalUserLimitExceeded,
    PortalUserRecord,
    authenticate_with_cognito,
    cognito_configured,
    complete_new_password_challenge,
    confirm_password_reset,
    create_portal_user,
    invite_portal_user,
    list_portal_users_for_client,
    portal_user_is_admin,
    request_password_reset,
    resolve_client_id,
    resolve_portal_role,
)
from hiveflow.project_config import get_environment_config, load_project_config


@pytest.fixture
def cognito_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_COGNITO_USER_POOL_ID", "us-east-2_TestPool")
    monkeypatch.setenv("HIVEFLOW_COGNITO_CLIENT_ID", "testclient")
    monkeypatch.setenv("HIVEFLOW_COGNITO_REGION", "us-east-2")
    monkeypatch.setenv("HIVEFLOW_PORTAL_DEFAULT_CLIENT_ID", "poc")
    monkeypatch.delenv("HIVEFLOW_PORTAL_USERNAME", raising=False)
    monkeypatch.delenv("HIVEFLOW_PORTAL_PASSWORD", raising=False)


def test_cognito_configured() -> None:
    assert cognito_configured() is False


def test_resolve_client_id_prefers_custom_attribute() -> None:
    client_id = resolve_client_id(
        {CLIENT_ID_ATTRIBUTE: "acme"},
        default_client_id="poc",
        username="user1",
    )
    assert client_id == "acme"


def test_resolve_portal_role_defaults_to_member() -> None:
    # Least-privilege default; real admins carry an explicit custom:portal_role.
    assert resolve_portal_role({}) == "member"
    assert resolve_portal_role({ROLE_ATTRIBUTE: "bogus"}) == "member"


def test_resolve_portal_role_honors_attribute() -> None:
    assert resolve_portal_role({ROLE_ATTRIBUTE: "member"}) == "member"
    assert resolve_portal_role({ROLE_ATTRIBUTE: "admin"}) == "admin"


def _mock_list_users_paginator(mock_client: MagicMock, users: list[dict] | None = None) -> None:
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Users": users or []}]
    mock_client.get_paginator.return_value = paginator


def test_authenticate_with_cognito_success(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_initiate_auth.return_value = {}
    mock_client.admin_get_user.return_value = {
        "Username": "poc",
        "UserAttributes": [
            {"Name": CLIENT_ID_ATTRIBUTE, "Value": "poc"},
        ],
    }

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        result = authenticate_with_cognito("poc", "SecretPass123!", company="POC", environment="dev")

    assert result is not None
    assert result.kind == "authenticated"
    assert result.user is not None
    assert result.user.username == "poc"
    assert result.user.client_id == "poc"


def test_authenticate_with_cognito_returns_new_password_challenge(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_initiate_auth.return_value = {
        "ChallengeName": "NEW_PASSWORD_REQUIRED",
        "Session": "session-token",
    }

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        result = authenticate_with_cognito("poc", "TempPass123!", company="POC", environment="dev")

    assert result is not None
    assert result.kind == "new_password"
    assert result.challenge == NewPasswordChallenge(username="poc", session="session-token")


def test_complete_new_password_challenge(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_respond_to_auth_challenge.return_value = {}
    mock_client.admin_get_user.return_value = {
        "Username": "poc",
        "UserAttributes": [{"Name": CLIENT_ID_ATTRIBUTE, "Value": "poc"}],
    }

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        user = complete_new_password_challenge(
            username="poc",
            session="session-token",
            new_password="BrandNewPass123!",
            company="POC",
            environment="dev",
        )

    assert user is not None
    assert user.username == "poc"
    mock_client.admin_respond_to_auth_challenge.assert_called_once()


def test_authenticate_uses_cognito_when_configured(cognito_env: None) -> None:
    login_result = PortalLoginResult(
        kind="authenticated",
        user=MagicMock(username="poc", client_id="poc"),
    )
    with patch(
        "hiveflow.dna.web.portal.cognito.authenticate_with_cognito",
        return_value=login_result,
    ) as mock_auth:
        user = authenticate("poc", "SecretPass123!", company="POC", environment="dev")

    assert user is not None
    assert user.username == "poc"
    mock_auth.assert_called_once()


def test_authenticate_falls_back_to_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")

    user = authenticate("poc", "changeme", company="POC", environment="dev")
    assert user is not None
    assert user.client_id == "poc"


def test_create_portal_user(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_create_user.return_value = {"User": {"UserStatus": "FORCE_CHANGE_PASSWORD"}}

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        result = create_portal_user(
            username="poc",
            password="SecretPass123!",
            client_id="poc",
            email="poc@example.com",
            company="POC",
            environment="dev",
        )

    assert result["username"] == "poc"
    assert result["delivery"] == "permanent"
    assert result["role"] == "admin"
    mock_client.admin_create_user.assert_called_once()
    assert mock_client.admin_create_user.call_args.kwargs["MessageAction"] == "SUPPRESS"
    attrs = {item["Name"]: item["Value"] for item in mock_client.admin_create_user.call_args.kwargs["UserAttributes"]}
    assert attrs[ROLE_ATTRIBUTE] == "admin"
    mock_client.admin_set_user_password.assert_called_once()


def test_invite_portal_user(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_create_user.return_value = {"User": {"UserStatus": "FORCE_CHANGE_PASSWORD"}}
    _mock_list_users_paginator(mock_client)

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        result = invite_portal_user(
            username="jane",
            client_id="poc",
            email="jane@example.com",
            company="POC",
            environment="dev",
            max_users=10,
        )

    assert result["delivery"] == "invite_email"
    assert result["email"] == "jane@example.com"
    assert result["role"] == "member"
    kwargs = mock_client.admin_create_user.call_args.kwargs
    assert "MessageAction" not in kwargs
    assert kwargs["DesiredDeliveryMediums"] == ["EMAIL"]
    attrs = {item["Name"]: item["Value"] for item in kwargs["UserAttributes"]}
    assert attrs[ROLE_ATTRIBUTE] == "member"
    mock_client.admin_set_user_password.assert_not_called()


def test_invite_portal_user_rejects_at_capacity(cognito_env: None) -> None:
    mock_client = MagicMock()
    existing = [
        {
            "Username": f"user{i}",
            "UserStatus": "CONFIRMED",
            "Enabled": True,
            "Attributes": [{"Name": CLIENT_ID_ATTRIBUTE, "Value": "poc"}],
        }
        for i in range(10)
    ]
    _mock_list_users_paginator(mock_client, existing)

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        with pytest.raises(PortalUserLimitExceeded):
            invite_portal_user(
                username="jane",
                client_id="poc",
                email="jane@example.com",
                company="POC",
                environment="dev",
                max_users=10,
            )

    mock_client.admin_create_user.assert_not_called()


def test_list_portal_users_for_client(cognito_env: None) -> None:
    mock_client = MagicMock()
    _mock_list_users_paginator(
        mock_client,
        [
            {
                "Username": "poc",
                "UserStatus": "CONFIRMED",
                "Enabled": True,
                "Attributes": [
                    {"Name": CLIENT_ID_ATTRIBUTE, "Value": "poc"},
                    {"Name": ROLE_ATTRIBUTE, "Value": "admin"},
                    {"Name": "email", "Value": "poc@example.com"},
                ],
            },
            {
                "Username": "other",
                "UserStatus": "CONFIRMED",
                "Enabled": True,
                "Attributes": [
                    {"Name": CLIENT_ID_ATTRIBUTE, "Value": "acme"},
                    {"Name": ROLE_ATTRIBUTE, "Value": "member"},
                ],
            },
        ],
    )

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        users = list_portal_users_for_client(client_id="poc", company="POC", environment="dev")

    paginator = mock_client.get_paginator.return_value
    paginator.paginate.assert_called_once_with(UserPoolId="us-east-2_TestPool")
    assert len(users) == 1
    assert users[0] == PortalUserRecord(
        username="poc",
        email="poc@example.com",
        client_id="poc",
        role="admin",
        status="CONFIRMED",
        enabled=True,
    )


def test_portal_user_is_admin(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_get_user.return_value = {
        "Username": "poc",
        "UserAttributes": [{"Name": ROLE_ATTRIBUTE, "Value": "admin"}],
    }

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        assert portal_user_is_admin("poc", company="POC", environment="dev") is True

    mock_client.admin_get_user.return_value = {
        "Username": "jane",
        "UserAttributes": [{"Name": ROLE_ATTRIBUTE, "Value": "member"}],
    }
    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        assert portal_user_is_admin("jane", company="POC", environment="dev") is False


def test_portal_user_is_admin_resolves_case_mismatch(cognito_env: None) -> None:
    mock_client = MagicMock()
    not_found = type("UserNotFoundException", (Exception,), {})
    mock_client.exceptions.UserNotFoundException = not_found

    def admin_get_user(**kwargs):
        username = kwargs["Username"]
        if username == "jeremy":
            raise not_found()
        if username == "Jeremy":
            return {
                "Username": "Jeremy",
                "UserAttributes": [{"Name": ROLE_ATTRIBUTE, "Value": "admin"}],
            }
        raise not_found()

    mock_client.admin_get_user.side_effect = admin_get_user
    _mock_list_users_paginator(
        mock_client,
        [
            {
                "Username": "Jeremy",
                "Attributes": [
                    {"Name": CLIENT_ID_ATTRIBUTE, "Value": "poc"},
                    {"Name": ROLE_ATTRIBUTE, "Value": "admin"},
                ],
                "UserStatus": "CONFIRMED",
                "Enabled": True,
            }
        ],
    )

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        assert portal_user_is_admin("jeremy", company="POC", environment="dev") is True


def test_authenticate_preserves_cognito_username_casing(cognito_env: None) -> None:
    mock_client = MagicMock()
    mock_client.admin_initiate_auth.return_value = {}
    mock_client.admin_get_user.return_value = {
        "Username": "Jeremy",
        "UserAttributes": [{"Name": CLIENT_ID_ATTRIBUTE, "Value": "poc"}],
    }

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        result = authenticate_with_cognito("Jeremy", "SecretPass123!", company="POC", environment="dev")

    assert result is not None
    assert result.user is not None
    assert result.user.username == "Jeremy"


def test_portal_login_shows_set_password_form(tmp_path, cognito_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "")

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(create_app(settings, company="POC", environment="dev", env_config=env_config))

    with patch(
        "hiveflow.dna.web.portal.cognito.authenticate_with_cognito",
        return_value=PortalLoginResult(
            kind="new_password",
            challenge=NewPasswordChallenge(username="poc", session="session-token"),
        ),
    ):
        response = client.post(
            "/portal/login",
            data={"action": "sign_in", "username": "poc", "password": "TempPass123!", "next": "/portal"},
        )

    assert response.status_code == 200
    assert b"Set your password" in response.data
    assert b'name="session" value="session-token"' in response.data


def test_portal_set_password_completes_login(tmp_path, cognito_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(create_app(settings, company="POC", environment="dev", env_config=env_config))

    with patch(
        "hiveflow.dna.web.portal.cognito.complete_new_password_challenge",
        return_value=MagicMock(username="poc", client_id="poc"),
    ):
        response = client.post(
            "/portal/login",
            data={
                "action": "set_password",
                "username": "poc",
                "session": "session-token",
                "new_password": "BrandNewPass123!",
                "confirm_password": "BrandNewPass123!",
                "next": "/portal",
            },
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/portal")


def _cognito_exceptions(mock_client: MagicMock) -> None:
    class _UserNotFound(Exception):
        pass

    class _InvalidParameter(Exception):
        pass

    class _LimitExceeded(Exception):
        pass

    class _NotAuthorized(Exception):
        pass

    class _CodeMismatch(Exception):
        pass

    class _ExpiredCode(Exception):
        pass

    class _InvalidPassword(Exception):
        pass

    mock_client.exceptions.UserNotFoundException = _UserNotFound
    mock_client.exceptions.InvalidParameterException = _InvalidParameter
    mock_client.exceptions.LimitExceededException = _LimitExceeded
    mock_client.exceptions.NotAuthorizedException = _NotAuthorized
    mock_client.exceptions.CodeMismatchException = _CodeMismatch
    mock_client.exceptions.ExpiredCodeException = _ExpiredCode
    mock_client.exceptions.InvalidPasswordException = _InvalidPassword


def test_request_password_reset_calls_cognito(cognito_env: None) -> None:
    mock_client = MagicMock()
    _cognito_exceptions(mock_client)

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        request_password_reset("poc", company="POC", environment="dev")

    mock_client.forgot_password.assert_called_once_with(
        ClientId="testclient",
        Username="poc",
    )


def test_request_password_reset_hides_unknown_user(cognito_env: None) -> None:
    mock_client = MagicMock()
    _cognito_exceptions(mock_client)
    mock_client.forgot_password.side_effect = mock_client.exceptions.UserNotFoundException()

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        request_password_reset("missing", company="POC", environment="dev")


def test_request_password_reset_rate_limit(cognito_env: None) -> None:
    mock_client = MagicMock()
    _cognito_exceptions(mock_client)
    mock_client.forgot_password.side_effect = mock_client.exceptions.LimitExceededException()

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        with pytest.raises(PasswordResetError, match="Too many reset attempts"):
            request_password_reset("poc", company="POC", environment="dev")


def test_confirm_password_reset(cognito_env: None) -> None:
    mock_client = MagicMock()
    _cognito_exceptions(mock_client)

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        confirm_password_reset(
            username="poc",
            confirmation_code="123456",
            new_password="BrandNewPass123!",
            company="POC",
            environment="dev",
        )

    mock_client.confirm_forgot_password.assert_called_once_with(
        ClientId="testclient",
        Username="poc",
        ConfirmationCode="123456",
        Password="BrandNewPass123!",
    )


def test_confirm_password_reset_rejects_bad_code(cognito_env: None) -> None:
    mock_client = MagicMock()
    _cognito_exceptions(mock_client)
    mock_client.confirm_forgot_password.side_effect = mock_client.exceptions.CodeMismatchException()

    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        with pytest.raises(PasswordResetError, match="incorrect"):
            confirm_password_reset(
                username="poc",
                confirmation_code="000000",
                new_password="BrandNewPass123!",
                company="POC",
                environment="dev",
            )


def test_portal_forgot_password_flow(tmp_path, cognito_env: None) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(create_app(settings, company="POC", environment="dev", env_config=env_config))

    get_response = client.get("/portal/login?mode=forgot_password")
    assert get_response.status_code == 200
    assert b"Forgot password" in get_response.data
    assert b"Send reset code" in get_response.data

    with patch("hiveflow.dna.web.portal.cognito.request_password_reset") as mock_request:
        response = client.post(
            "/portal/login",
            data={"action": "forgot_password", "username": "poc", "next": "/portal"},
        )

    mock_request.assert_called_once()
    assert response.status_code == 200
    assert b"Reset your password" in response.data
    assert b"reset code" in response.data.lower()
    assert b'name="username" value="poc"' in response.data


def test_portal_confirm_forgot_password_returns_to_sign_in(
    tmp_path, cognito_env: None
) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(create_app(settings, company="POC", environment="dev", env_config=env_config))

    with patch("hiveflow.dna.web.portal.cognito.confirm_password_reset") as mock_confirm:
        response = client.post(
            "/portal/login",
            data={
                "action": "confirm_forgot_password",
                "username": "poc",
                "confirmation_code": "123456",
                "new_password": "BrandNewPass123!",
                "confirm_password": "BrandNewPass123!",
                "next": "/portal",
            },
        )

    mock_confirm.assert_called_once()
    assert response.status_code == 200
    assert b"Sign in to HiveFlowAI" in response.data
    assert b"Password updated" in response.data


def test_portal_login_includes_forgot_password_link(tmp_path, cognito_env: None) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(create_app(settings, company="POC", environment="dev", env_config=env_config))

    response = client.get("/portal/login")
    assert response.status_code == 200
    assert b"Forgot password?" in response.data
    assert b"mode=forgot_password" in response.data


def test_global_portal_admin_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    from hiveflow.dna.web.portal.auth import (
        PortalSession,
        effective_portal_client_id,
        is_global_portal_admin,
    )

    monkeypatch.setenv("HIVEFLOW_ADMIN_USERNAME", "GlobalAdmin")
    session = PortalSession(username="GlobalAdmin", client_id="platform", issued_at=1)
    assert is_global_portal_admin(username="GlobalAdmin", client_id="platform") is True
    assert is_global_portal_admin(username="GlobalAdmin", client_id="poc") is False
    assert effective_portal_client_id(session, fixed_client_id="poc") == "platform"


def test_portal_user_is_admin_for_global_admin(cognito_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("HIVEFLOW_ADMIN_USERNAME", "GlobalAdmin")
    mock_client = MagicMock()
    mock_client.admin_get_user.return_value = {
        "Username": "GlobalAdmin",
        "UserAttributes": [
            {"Name": CLIENT_ID_ATTRIBUTE, "Value": "platform"},
            {"Name": ROLE_ATTRIBUTE, "Value": "admin"},
        ],
    }
    with patch("hiveflow.dna.web.portal.cognito._cognito_client", return_value=mock_client):
        assert portal_user_is_admin("GlobalAdmin", company="POC", environment="dev") is True
