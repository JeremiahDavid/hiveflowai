"""Onboarding wizard business logic for platform admin."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from hiveflow.client_registry import (
    CLIENT_ID_RE,
    ClientCreateSpec,
    ClientRegistry,
    ConnectorSchedule,
    ConnectorSpec,
    DnaSpec,
    PortalClientSpec,
    SUPPORTED_CONNECTORS,
    merge_stack_status_with_build,
    verify_post_deploy,
)
from hiveflow.project_config import (
    get_environment_config,
    get_platform_environment_config,
    get_ui_domain_config,
    global_dns_stack_name,
    is_global_dns_stack_enabled,
    iter_configured_connectors,
    refresh_platform_config,
    reporting_stack_name,
    resolve_reporting_site_url,
)
from hiveflow.provisioning import get_build_status, start_client_deploy
from hiveflow.secrets_manager import ensure_secret_json, get_secret_json, put_secret_json


def _onboarding_registry() -> ClientRegistry:
    refresh_platform_config()
    return ClientRegistry()


def get_onboarding_client(
    company: str,
    *,
    environment: str,
    client_id: str | None = None,
):
    registry = _onboarding_registry()
    return registry.get_client(company, environment=environment, client_id=client_id)


def list_onboarding_clients(*, environment: str | None = None) -> list[dict[str, Any]]:
    registry = _onboarding_registry()
    return [asdict(record) for record in registry.list_clients(environment=environment)]


def company_from_display_name(display_name: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", display_name.strip()) if part]
    if not parts:
        raise ValueError("Display name must include at least one letter or number")
    company = "".join(part.lower() for part in parts)
    if not company[0].isalpha():
        company = f"c{company}"
    return company[:63]


_CONNECTOR_DEFAULTS: dict[str, dict[str, str | int]] = {
    "dbc": {"entity_bundle": "full", "schedule_hour": 6, "schedule_minute": 30},
    "qbo": {"entity_bundle": "full_accounting", "schedule_hour": 6, "schedule_minute": 0, "tier": "sandbox"},
    "qbd": {"entity_bundle": "full_accounting"},
}

WIZARD_STEP_COUNT = 4

ONBOARDING_STEP_LABELS: dict[int, str] = {
    1: "Client config",
    2: "Connectors",
    3: "Deploy",
    4: "Pipelines",
}

# Backwards-compatible aliases used by views.
WIZARD_STEP_LABELS = ONBOARDING_STEP_LABELS
DETAIL_STEP_LABEL = ONBOARDING_STEP_LABELS[2]


# Bundle names must match hiveflow.{bc,qbo,qbd}.entities registry keys.
_CONNECTOR_ENTITY_BUNDLES: dict[str, tuple[str, ...]] = {
    "dbc": ("full", "v1_accounting", "v1_intra"),
    "qbo": ("full_accounting", "v1_accounting"),
    "qbd": ("full_accounting", "v1_accounting"),
}


def entity_bundles_for_connector(source: str) -> list[str]:
    return list(_CONNECTOR_ENTITY_BUNDLES.get(source.strip().lower(), ()))


def _form_enabled(value: str) -> bool:
    return value.strip().lower() in {"on", "true", "1", "yes"}


def parse_initial_admin_fields(form: dict[str, str]) -> tuple[str | None, str | None]:
    username = str(form.get("initial_admin_username", "")).strip() or None
    email = str(form.get("initial_admin_email", "")).strip() or None
    if bool(username) != bool(email):
        raise ValueError("Initial portal admin username and email must both be provided or both left empty")
    return username, email


def initial_admin_from_config(
    *,
    company: str,
    environment: str,
    client_id: str,
) -> dict[str, str]:
    registry = _onboarding_registry()
    record = registry.get_client(company, environment=environment, client_id=client_id)
    if record is None:
        return {}

    platform_env = get_platform_environment_config(record.environment)
    ui_cfg = platform_env.get("ui", {})
    portal_cfg = ui_cfg.get("portal", {}) if isinstance(ui_cfg, dict) else {}
    clients = portal_cfg.get("clients", {}) if isinstance(portal_cfg, dict) else {}
    client_cfg = clients.get(record.client_id, {}) if isinstance(clients, dict) else {}
    if not isinstance(client_cfg, dict):
        return {}

    values: dict[str, str] = {}
    username = str(client_cfg.get("initial_admin_username", "")).strip()
    email = str(client_cfg.get("initial_admin_email", "")).strip()
    if username:
        values["initial_admin_username"] = username
    if email:
        values["initial_admin_email"] = email
    return values


def client_portal_site_urls(
    *,
    environment: str,
    client_id: str,
) -> dict[str, str]:
    """Resolve public portal URLs from platform UI domain config."""
    normalized_client_id = client_id.strip().lower()
    if not normalized_client_id:
        return {}

    try:
        platform_env = get_platform_environment_config(environment)
    except KeyError:
        return {}

    ui_cfg = platform_env.get("ui", {})
    portal_cfg = ui_cfg.get("portal", {}) if isinstance(ui_cfg, dict) else {}
    clients = portal_cfg.get("clients", {}) if isinstance(portal_cfg, dict) else {}
    client_cfg = clients.get(normalized_client_id, {})
    if not isinstance(client_cfg, dict):
        client_cfg = {}

    domain_cfg = get_ui_domain_config(platform_env)
    site_url = resolve_reporting_site_url(domain_cfg, client_cfg, normalized_client_id)
    if not site_url:
        return {}

    base = site_url.rstrip("/")
    return {
        "portal": f"{base}/portal",
        "login": f"{base}/portal/login",
        "governance_users": f"{base}/portal/governance/users",
    }


def parse_connectors_from_form(form: dict[str, str]) -> tuple[ConnectorSpec, ...]:
    connectors: list[ConnectorSpec] = []
    for source in sorted(SUPPORTED_CONNECTORS):
        if not _form_enabled(str(form.get(f"connector_{source}_enabled", ""))):
            continue
        defaults = _CONNECTOR_DEFAULTS[source]
        schedule = None
        if source in {"qbo", "dbc"}:
            schedule = ConnectorSchedule(
                hour=int(form.get(f"connector_{source}_schedule_hour", str(defaults["schedule_hour"])) or 6),
                minute=int(form.get(f"connector_{source}_schedule_minute", str(defaults["schedule_minute"])) or 0),
            )
        tier = None
        if source == "qbo":
            tier = str(form.get("connector_qbo_tier", str(defaults.get("tier", "")))).strip() or None
        connectors.append(
            ConnectorSpec(
                source=source,
                entity_bundle=str(
                    form.get(f"connector_{source}_entity_bundle", str(defaults["entity_bundle"]))
                ).strip(),
                schedule=schedule,
                tier=tier,
            )
        )
    if not connectors:
        raise ValueError("At least one connector must be enabled")
    return tuple(connectors)


def normalize_wizard_step(step: int) -> int:
    return max(1, min(WIZARD_STEP_COUNT, int(step)))


def validate_client_config_form(form: dict[str, str]) -> None:
    display_name = str(form.get("display_name", "")).strip()
    client_id = str(form.get("client_id", "")).strip().lower()
    if not display_name:
        raise ValueError("Display name is required")
    if not client_id:
        raise ValueError("Portal client id is required")
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError("Portal client id must start with a letter and use lowercase letters and numbers only")
    company_from_display_name(display_name)
    parse_connectors_from_form(form)
    parse_initial_admin_fields(form)


def parse_client_create_form(form: dict[str, str]) -> ClientCreateSpec:
    display_name = str(form.get("display_name", "")).strip()
    client_id = str(form.get("client_id", "")).strip().lower()
    existing_company = str(form.get("onboarding_company", "")).strip().lower()
    existing_environment = str(form.get("onboarding_environment", "")).strip().lower()
    connectors = parse_connectors_from_form(form)
    initial_admin_username, initial_admin_email = parse_initial_admin_fields(form)
    connector_sources = [item.source for item in connectors]
    dna_source = "dbc" if "dbc" in connector_sources else connector_sources[0]
    dna_schedule = None
    return ClientCreateSpec(
        company=existing_company or company_from_display_name(display_name),
        client_id=client_id,
        environment=existing_environment or "dev",
        connectors=connectors,
        dna=DnaSpec(
            enabled=form.get("dna_enabled", "on") in {"on", "true", "1", "yes"},
            source=str(form.get("dna_source", dna_source)).strip().lower(),
            schedule=dna_schedule,
        ),
        portal=PortalClientSpec(
            display_name=display_name,
            reporting_hostname=client_id,
            welcome_title=str(form.get("welcome_title", "")).strip() or "Your operational dashboard",
            welcome_message=str(form.get("welcome_message", "")).strip(),
            initial_admin_username=initial_admin_username,
            initial_admin_email=initial_admin_email,
        ),
        aws_region=str(form.get("aws_region", "us-east-2")).strip() or "us-east-2",
    )


def client_config_form_values(
    *,
    company: str,
    environment: str,
    client_id: str,
) -> dict[str, str]:
    registry = _onboarding_registry()
    record = registry.get_client(company, environment=environment, client_id=client_id)
    if record is None:
        raise ValueError(f"Client {company}/{client_id} not found")

    env_config = get_environment_config(record.company, record.environment)
    platform_env = get_platform_environment_config(record.environment)
    ui_cfg = platform_env.get("ui", {})
    portal_cfg = ui_cfg.get("portal", {}) if isinstance(ui_cfg, dict) else {}
    clients = portal_cfg.get("clients", {}) if isinstance(portal_cfg, dict) else {}
    client_cfg = clients.get(record.client_id, {}) if isinstance(clients, dict) else {}

    values: dict[str, str] = {
        "onboarding_company": record.company,
        "onboarding_environment": record.environment,
        "onboarding_client_id": record.client_id,
        "display_name": record.portal_display_name,
        "client_id": record.client_id,
    }
    if isinstance(client_cfg, dict):
        welcome_title = str(client_cfg.get("welcome_title", "")).strip()
        welcome_message = str(client_cfg.get("welcome_message", "")).strip()
        if welcome_title:
            values["welcome_title"] = welcome_title
        if welcome_message:
            values["welcome_message"] = welcome_message
        initial_admin_username = str(client_cfg.get("initial_admin_username", "")).strip()
        initial_admin_email = str(client_cfg.get("initial_admin_email", "")).strip()
        if initial_admin_username:
            values["initial_admin_username"] = initial_admin_username
        if initial_admin_email:
            values["initial_admin_email"] = initial_admin_email

    for source, cfg in iter_configured_connectors(env_config):
        if not isinstance(cfg, dict):
            continue
        values[f"connector_{source}_enabled"] = "on"
        entity_bundle = str(cfg.get("entity_bundle", "")).strip()
        if entity_bundle:
            values[f"connector_{source}_entity_bundle"] = entity_bundle
        schedule = cfg.get("schedule", {})
        if isinstance(schedule, dict):
            if schedule.get("hour") is not None:
                values[f"connector_{source}_schedule_hour"] = str(schedule["hour"])
            if schedule.get("minute") is not None:
                values[f"connector_{source}_schedule_minute"] = str(schedule["minute"])
        if source == "qbo":
            tier = str(cfg.get("tier", "")).strip()
            if tier:
                values[f"connector_{source}_tier"] = tier

    dna_cfg = env_config.get("dna", {})
    if isinstance(dna_cfg, dict):
        if dna_cfg.get("enabled"):
            values["dna_enabled"] = "on"
        dna_source = str(dna_cfg.get("source", "")).strip()
        if dna_source:
            values["dna_source"] = dna_source

    aws_cfg = env_config.get("aws", {})
    if isinstance(aws_cfg, dict):
        region = str(aws_cfg.get("region", "")).strip()
        if region:
            values["aws_region"] = region

    return values


def save_client_from_form(form: dict[str, str]) -> dict[str, Any]:
    spec = parse_client_create_form(form)
    registry = _onboarding_registry()
    existing_company = str(form.get("onboarding_company", "")).strip().lower()
    if existing_company:
        record = registry.update_client(spec)
    else:
        record = registry.create_client(spec)
    return {"ok": True, "client": asdict(record)}


def create_client_from_form(form: dict[str, str]) -> dict[str, Any]:
    return save_client_from_form(form)


@dataclass(frozen=True)
class ConnectorCredentialSnapshot:
    secret_id: str
    exists: bool
    values: dict[str, str]
    error: str | None = None


def _credential_field_keys(source: str) -> frozenset[str]:
    from hiveflow.dna.web.admin.onboarding.guides import CONNECTOR_CREDENTIAL_FIELDS

    fields = CONNECTOR_CREDENTIAL_FIELDS.get(source.strip().lower(), ())
    return frozenset(field.key for field in fields)


def _meaningful_credential_value(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return text and text != "REPLACE_ME"


def load_connector_credentials(
    *,
    company: str,
    environment: str,
    source: str,
    region: str | None = None,
) -> ConnectorCredentialSnapshot:
    """Load saved connector credential fields from Secrets Manager for display."""
    registry = _onboarding_registry()
    record = registry.get_client(company, environment=environment)
    if record is None:
        raise ValueError(f"No portal client found for company {company!r}")

    secret_id = registry.secret_name(record, source=source)
    field_keys = _credential_field_keys(source)
    try:
        payload = get_secret_json(secret_id, region=region)
    except ValueError as exc:
        message = str(exc)
        if "was not found" in message:
            return ConnectorCredentialSnapshot(secret_id=secret_id, exists=False, values={})
        return ConnectorCredentialSnapshot(
            secret_id=secret_id,
            exists=False,
            values={},
            error=message,
        )

    values = {
        key: str(payload[key]).strip()
        for key in field_keys
        if key in payload and _meaningful_credential_value(payload[key])
    }
    return ConnectorCredentialSnapshot(secret_id=secret_id, exists=True, values=values)


def load_client_connector_credentials(
    *,
    company: str,
    environment: str,
    sources: list[str] | tuple[str, ...],
    region: str | None = None,
) -> dict[str, ConnectorCredentialSnapshot]:
    snapshots: dict[str, ConnectorCredentialSnapshot] = {}
    for source in sources:
        source_key = source.strip().lower()
        if not source_key:
            continue
        try:
            snapshots[source_key] = load_connector_credentials(
                company=company,
                environment=environment,
                source=source_key,
                region=region,
            )
        except ValueError as exc:
            snapshots[source_key] = ConnectorCredentialSnapshot(
                secret_id="",
                exists=False,
                values={},
                error=str(exc),
            )
    return snapshots


def save_connector_secret(
    *,
    company: str,
    environment: str,
    source: str,
    credentials: dict[str, str],
    region: str | None = None,
) -> dict[str, Any]:
    registry = _onboarding_registry()
    record = registry.get_client(company, environment=environment)
    if record is None:
        raise ValueError(f"No portal client found for company {company!r}")

    secret_id = registry.secret_name(record, source=source)
    payload = {key: str(value).strip() for key, value in credentials.items() if str(value).strip()}
    result = ensure_secret_json(
        secret_id,
        payload,
        region=region,
        source=source,
        company=company,
        environment=environment,
    )
    if result == "exists":
        existing = get_secret_json(secret_id, region=region)
        merged = dict(existing)
        merged.update(payload)
        put_secret_json(secret_id, merged, region=region)
    return {"secret_id": secret_id, "result": result}


def validate_connector(
    *,
    source: str,
    credentials: dict[str, str],
    secret_id: str | None = None,
    region: str | None = None,
    pre_deploy: bool = False,
) -> dict[str, Any]:
    source_key = source.strip().lower()
    if source_key == "dbc":
        from hiveflow.connectors.onboarding.dbc import validate_dbc_credentials

        return validate_dbc_credentials(credentials)

    if source_key == "qbo":
        from hiveflow.connectors.onboarding.qbo import qbo_oauth_status

        payload = credentials
        if secret_id:
            try:
                payload = get_secret_json(secret_id, region=region)
            except ValueError:
                pass
        return qbo_oauth_status(payload)

    if source_key == "qbd":
        from hiveflow.connectors.onboarding.qbd import qbd_secret_status

        payload = credentials
        if secret_id:
            try:
                payload = get_secret_json(secret_id, region=region)
            except ValueError:
                pass
        return qbd_secret_status(payload, require_soap_url=not pre_deploy)

    return {"ok": False, "error": f"Unsupported connector {source!r}"}


def connectors_ready_for_deploy(
    *,
    company: str,
    environment: str,
    client_id: str,
    region: str | None = None,
) -> dict[str, Any]:
    registry = _onboarding_registry()
    record = registry.get_client(company, environment=environment, client_id=client_id)
    if record is None:
        return {
            "ok": False,
            "message": f"Client {company}/{client_id} not found",
            "connectors": {},
        }

    connector_results: dict[str, Any] = {}
    failures: list[str] = []
    for source in record.connector_sources:
        snapshot = load_connector_credentials(
            company=record.company,
            environment=environment,
            source=source,
            region=region,
        )
        if not snapshot.exists:
            connector_results[source] = {"ok": False, "error": "Save connector secret first."}
            failures.append(f"{source}: secret not saved")
            continue
        result = validate_connector(
            source=source,
            credentials=snapshot.values,
            secret_id=snapshot.secret_id,
            region=region,
            pre_deploy=True,
        )
        connector_results[source] = result
        if not result.get("ok"):
            detail = str(result.get("error") or result.get("message") or "validation failed")
            failures.append(f"{source}: {detail}")

    if failures:
        return {
            "ok": False,
            "connectors": connector_results,
            "message": "Validate all connectors before deploying. " + "; ".join(failures),
        }
    return {"ok": True, "connectors": connector_results}


def trigger_deploy(
    *,
    company: str,
    environment: str,
    client_id: str,
    scope: str = "all",
    region: str | None = None,
) -> dict[str, Any]:
    readiness = connectors_ready_for_deploy(
        company=company,
        environment=environment,
        client_id=client_id,
        region=region,
    )
    if not readiness.get("ok"):
        return {
            "status": "blocked",
            "ok": False,
            "message": str(readiness.get("message") or "Validate all connectors before deploying."),
            "connectors": readiness.get("connectors", {}),
        }
    result = start_client_deploy(
        company=company,
        environment=environment,
        client_id=client_id,
        scope=scope,
        region=region,
    )
    result["ok"] = result.get("status") not in {"misconfigured", "blocked"}
    return result


def list_connector_companies(*, source: str, credentials: dict[str, str]) -> dict[str, Any]:
    source_key = source.strip().lower()
    if source_key == "dbc":
        from hiveflow.connectors.onboarding.dbc import list_dbc_companies

        return list_dbc_companies(credentials)
    return {"ok": False, "error": f"Company lookup not supported for {source!r}"}


def client_deploy_status(
    *,
    company: str,
    environment: str,
    client_id: str,
    region: str | None = None,
    build_id: str | None = None,
) -> dict[str, Any]:
    registry = _onboarding_registry()
    record = registry.get_client(company, environment=environment, client_id=client_id)
    if record is None:
        raise ValueError(f"Client {company}/{client_id} not found")
    status = registry.get_deploy_status(record, region=region)
    stacks = list(status.stacks)
    build_payload: dict[str, Any] | None = None
    resolved_build_id = str(build_id or "").strip()
    if resolved_build_id:
        build_payload = get_build_status(resolved_build_id, region=region)
        stacks = merge_stack_status_with_build(stacks, build_payload)
    verification = verify_post_deploy(record, region=region)
    payload: dict[str, Any] = {
        "deploy": {
            "company": status.company,
            "client_id": status.client_id,
            "environment": status.environment,
            "stacks": [
                {
                    "stack_name": item.stack_name,
                    "status": item.status.value,
                    "status_reason": item.status_reason,
                    "events": item.events,
                }
                for item in stacks
            ],
        },
        "verification": verification,
    }
    if build_payload is not None:
        payload["build"] = build_payload
    payload["portal_urls"] = client_portal_site_urls(
        environment=record.environment,
        client_id=record.client_id,
    )
    return payload


def generate_qwc_download(
    *,
    soap_url: str,
    username: str,
    app_name: str = "HiveFlow QBD",
) -> str:
    from hiveflow.connectors.onboarding.qbd import generate_qwc_xml

    return generate_qwc_xml(
        app_name=app_name,
        soap_url=soap_url,
        username=username,
    )


def build_status(build_id: str, *, region: str | None = None) -> dict[str, Any]:
    return get_build_status(build_id, region=region)


def reporting_stack_deployed(
    *,
    client_id: str,
    environment: str,
    status_payload: dict[str, Any] | None,
) -> bool:
    expected = reporting_stack_name(client_id, environment).lower()
    stacks = (status_payload or {}).get("deploy", {}).get("stacks", [])
    for item in stacks:
        if not isinstance(item, dict):
            continue
        stack_name = str(item.get("stack_name", "")).strip().lower()
        if stack_name != expected:
            continue
        return str(item.get("status", "")).strip().lower() == "complete"
    return False


def dna_stack_deployed(
    *,
    company: str,
    environment: str,
    status_payload: dict[str, Any] | None,
) -> bool:
    """A client is portal-ready once its data plane (DnaStack) is deployed.

    The reporting UI is the shared PortalStack and DNS is the ``*.{zone}``
    wildcard — neither is provisioned per client any more.
    """
    env_slug = environment.strip().lower()
    candidates = {f"dnastack-{company.strip().lower()}-{env_slug}"}
    try:
        from hiveflow.project_config import dna_stack_name

        candidates.add(dna_stack_name(company, environment).lower())
    except KeyError:
        pass
    stacks = (status_payload or {}).get("deploy", {}).get("stacks", [])
    for item in stacks:
        if not isinstance(item, dict):
            continue
        if str(item.get("stack_name", "")).strip().lower() not in candidates:
            continue
        return str(item.get("status", "")).strip().lower() == "complete"
    return False


def global_dns_stack_deployed(
    *,
    environment: str,
    status_payload: dict[str, Any] | None,
) -> bool:
    expected = global_dns_stack_name(environment).lower()
    stacks = (status_payload or {}).get("deploy", {}).get("stacks", [])
    for item in stacks:
        if not isinstance(item, dict):
            continue
        stack_name = str(item.get("stack_name", "")).strip().lower()
        if stack_name != expected:
            continue
        return str(item.get("status", "")).strip().lower() == "complete"
    return False


def portal_dns_required(*, environment: str) -> bool:
    try:
        platform_env = get_platform_environment_config(environment)
    except KeyError:
        return False
    return is_global_dns_stack_enabled(platform_env)


def portal_deploy_ready(
    *,
    client_id: str,
    environment: str,
    status_payload: dict[str, Any] | None,
    company: str | None = None,
) -> bool:
    # Readiness is now just the client's data plane; the shared PortalStack and
    # wildcard DNS are environment-level, not per client.
    return dna_stack_deployed(
        company=(company or client_id),
        environment=environment,
        status_payload=status_payload,
    )


def invite_onboarding_admin(
    *,
    company: str,
    environment: str,
    client_id: str,
    username: str,
    email: str,
    status_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_username = username.strip()
    normalized_email = email.strip()
    if not normalized_username and not normalized_email:
        return {"ok": True, "skipped": True, "message": "No admin invite requested."}
    if not normalized_username:
        raise ValueError("Initial admin username is required when email is provided")
    if not normalized_email:
        raise ValueError("Initial admin email is required when username is provided")
    if not portal_deploy_ready(
        client_id=client_id,
        environment=environment,
        status_payload=status_payload,
        company=company,
    ):
        raise ValueError("Deploy the client's DnaStack before inviting a portal admin.")

    from hiveflow.dna.web.portal.cognito import PORTAL_ROLE_ADMIN, invite_portal_user
    from hiveflow.dna.web.portal.config import load_client_portal_config

    platform_env = get_platform_environment_config(environment)
    ui_cfg = platform_env.get("ui", {})
    default_pack_id = str(ui_cfg.get("pack_id", "dbc_dna_boilerplate")) if isinstance(ui_cfg, dict) else "dbc_dna_boilerplate"
    portal_client = load_client_portal_config(
        client_id,
        platform_env,
        default_pack_id=default_pack_id,
    )
    invite = invite_portal_user(
        username=normalized_username,
        client_id=client_id,
        email=normalized_email,
        company=company,
        environment=environment,
        max_users=portal_client.max_users,
        role=PORTAL_ROLE_ADMIN,
    )
    return {
        "ok": True,
        "invite": invite,
        "message": f"Invite sent to {normalized_email} as portal admin.",
    }
