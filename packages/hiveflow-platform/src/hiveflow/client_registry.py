"""Client onboarding registry — read/write config.yaml entries and deploy status."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from hiveflow.project_config import (
    default_config_path,
    dna_stack_name,
    get_environment_config,
    get_platform_environment_config,
    ingest_stack_name,
    is_dna_stack_enabled,
    iter_configured_connectors,
    iter_portal_reporting_clients,
    load_project_config,
    resolve_qbo_secret_name,
    save_project_config,
)

CLIENT_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,62}$")
CLIENT_ID_HTML_PATTERN = "[a-z][a-z0-9]{0,62}"
COMPANY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
SUPPORTED_CONNECTORS = frozenset({"dbc", "qbo", "qbd"})


class StackLifecycle(str, Enum):
    NOT_FOUND = "not_found"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConnectorSchedule:
    hour: int = 6
    minute: int = 0


@dataclass(frozen=True)
class ConnectorSpec:
    source: str
    entity_bundle: str
    schedule: ConnectorSchedule | None = None
    tier: str | None = None


@dataclass(frozen=True)
class DnaSpec:
    enabled: bool = True
    source: str = "dbc"
    schedule: ConnectorSchedule | None = None


@dataclass(frozen=True)
class PortalClientSpec:
    display_name: str
    reporting_hostname: str
    welcome_title: str = "Your operational dashboard"
    welcome_message: str = ""
    accent_color: str | None = None
    max_users: int | None = None
    initial_admin_username: str | None = None
    initial_admin_email: str | None = None


@dataclass(frozen=True)
class ClientCreateSpec:
    company: str
    client_id: str
    environment: str
    connectors: tuple[ConnectorSpec, ...]
    dna: DnaSpec
    portal: PortalClientSpec
    aws_region: str = "us-east-2"


@dataclass(frozen=True)
class ClientRecord:
    company: str
    client_id: str
    environment: str
    connector_sources: tuple[str, ...]
    portal_display_name: str
    reporting_hostname: str
    dna_enabled: bool

    @property
    def connector_source(self) -> str:
        return self.connector_sources[0] if self.connector_sources else ""


@dataclass
class StackDeployStatus:
    stack_name: str
    status: StackLifecycle
    status_reason: str = ""
    events: list[dict[str, str]] = field(default_factory=list)


@dataclass
class DeployStatus:
    company: str
    client_id: str
    environment: str
    stacks: list[StackDeployStatus]


def _normalize_company(company: str) -> str:
    return company.strip().lower()


def _normalize_client_id(client_id: str) -> str:
    return client_id.strip().lower()


def _schedule_dict(schedule: ConnectorSchedule | None) -> dict[str, int] | None:
    if schedule is None:
        return None
    return {"hour": int(schedule.hour), "minute": int(schedule.minute)}


def validate_client_create_spec(spec: ClientCreateSpec, *, path: Path | None = None) -> None:
    company = _normalize_company(spec.company)
    client_id = _normalize_client_id(spec.client_id)
    environment = spec.environment.strip().lower()
    if not spec.connectors:
        raise ValueError("At least one connector is required")

    connector_sources = [item.source.strip().lower() for item in spec.connectors]
    if len(connector_sources) != len(set(connector_sources)):
        raise ValueError("Duplicate connector sources are not allowed")

    if not COMPANY_RE.match(company):
        raise ValueError(
            f"company must be lowercase alphanumeric (got {company!r}); "
            "example: acme"
        )
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError(
            f"client_id must be lowercase letters and numbers only (got {client_id!r}); "
            "example: acme"
        )
    if not environment:
        raise ValueError("environment is required")
    for connector in spec.connectors:
        source = connector.source.strip().lower()
        if source not in SUPPORTED_CONNECTORS:
            raise ValueError(f"Unsupported connector {source!r}; expected one of {sorted(SUPPORTED_CONNECTORS)}")
        if not connector.entity_bundle.strip():
            raise ValueError(f"connector.entity_bundle is required for {source}")
    if not spec.portal.display_name.strip():
        raise ValueError("portal.display_name is required")
    if not spec.portal.reporting_hostname.strip():
        raise ValueError("portal.reporting_hostname is required")

    config = load_project_config(path)
    companies = config.get("companies", {})
    if isinstance(companies, dict) and company in companies:
        raise ValueError(f"Company {company!r} already exists in config.yaml")

    platform_env = get_platform_environment_config(environment, path=path)
    ui_cfg = platform_env.get("ui", {})
    portal_cfg = ui_cfg.get("portal", {}) if isinstance(ui_cfg, dict) else {}
    clients = portal_cfg.get("clients", {}) if isinstance(portal_cfg, dict) else {}
    if isinstance(clients, dict) and client_id in clients:
        raise ValueError(f"Portal client {client_id!r} already exists in config.yaml")

    if spec.dna.enabled:
        dna_source = spec.dna.source.strip().lower()
        if dna_source not in connector_sources:
            raise ValueError("dna.source must match one of the configured connectors")


def iter_portal_clients_missing_reporting_company(
    config: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """Yield ``(environment, client_id, reason)`` for every misconfigured portal client.

    Multi-tenant reporting binds each portal client to its data plane via
    ``reporting_company``; an empty value, or one with no ``companies.<name>``
    block, cannot be served.
    """
    problems: list[tuple[str, str, str]] = []
    companies = config.get("companies", {})
    companies = companies if isinstance(companies, dict) else {}
    platform_envs = (config.get("platform", {}) or {}).get("environments", {})
    platform_envs = platform_envs if isinstance(platform_envs, dict) else {}
    for env_name, platform_env in platform_envs.items():
        if not isinstance(platform_env, dict):
            continue
        ui_cfg = platform_env.get("ui", {})
        portal_cfg = ui_cfg.get("portal", {}) if isinstance(ui_cfg, dict) else {}
        clients = portal_cfg.get("clients", {}) if isinstance(portal_cfg, dict) else {}
        if not isinstance(clients, dict):
            continue
        for client_id, raw in clients.items():
            if client_id == "default":
                continue
            reporting_company = ""
            if isinstance(raw, dict):
                reporting_company = str(raw.get("reporting_company", "")).strip()
            if not reporting_company:
                problems.append((str(env_name), str(client_id), "missing reporting_company"))
                continue
            company_cfg = companies.get(reporting_company)
            company_envs = (
                company_cfg.get("environments", {}) if isinstance(company_cfg, dict) else {}
            )
            if not isinstance(company_envs, dict) or env_name not in company_envs:
                problems.append(
                    (
                        str(env_name),
                        str(client_id),
                        f"reporting_company {reporting_company!r} has no companies.{reporting_company}"
                        f".environments.{env_name} block",
                    )
                )
    return problems


def _assert_portal_client_reporting_company(
    config: dict[str, Any], *, environment: str, client_id: str
) -> None:
    for env_name, cid, reason in iter_portal_clients_missing_reporting_company(config):
        if env_name == environment and cid == client_id:
            raise ValueError(f"Portal client {client_id!r} ({environment}): {reason}")


def _connector_block(spec: ConnectorSpec) -> dict[str, Any]:
    source = spec.source.strip().lower()
    block: dict[str, Any] = {"entity_bundle": spec.entity_bundle.strip()}
    schedule = _schedule_dict(spec.schedule)
    if schedule and source in {"qbo", "dbc"}:
        block["schedule"] = schedule
    if source == "qbo" and spec.tier:
        block["tier"] = spec.tier.strip()
    return block


def _dna_block(spec: DnaSpec, connector_source: str) -> dict[str, Any]:
    block: dict[str, Any] = {
        "enabled": bool(spec.enabled),
        "source": spec.source.strip().lower() or connector_source,
    }
    schedule = _schedule_dict(spec.schedule)
    if schedule:
        block["schedule"] = schedule
    return block


def _portal_client_block(spec: ClientCreateSpec) -> dict[str, Any]:
    company = _normalize_company(spec.company)
    block: dict[str, Any] = {
        "display_name": spec.portal.display_name.strip(),
        "reporting_company": company,
        "reporting_hostname": spec.portal.reporting_hostname.strip().lower(),
        "welcome_title": spec.portal.welcome_title.strip() or "Your operational dashboard",
    }
    if spec.portal.max_users is not None:
        block["max_users"] = int(spec.portal.max_users)
    accent_color = str(spec.portal.accent_color or "").strip()
    if accent_color:
        block["accent_color"] = accent_color
    message = spec.portal.welcome_message.strip()
    if message:
        block["welcome_message"] = message
    initial_admin_username = str(spec.portal.initial_admin_username or "").strip()
    if initial_admin_username:
        block["initial_admin_username"] = initial_admin_username
    initial_admin_email = str(spec.portal.initial_admin_email or "").strip()
    if initial_admin_email:
        block["initial_admin_email"] = initial_admin_email
    return block


def build_company_environment_config(spec: ClientCreateSpec) -> dict[str, Any]:
    env_config: dict[str, Any] = {
        "aws": {"region": spec.aws_region.strip() or "us-east-2"},
    }
    for connector in spec.connectors:
        source = connector.source.strip().lower()
        env_config[source] = _connector_block(connector)
    if spec.dna.enabled:
        primary_source = spec.connectors[0].source.strip().lower()
        env_config["dna"] = _dna_block(spec.dna, primary_source)
    return env_config


class ClientRegistry:
    """Read and write per-client onboarding configuration from config.yaml."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path

    @property
    def config_path(self) -> Path:
        return self._path or default_config_path()

    def list_clients(self, *, environment: str | None = None) -> list[ClientRecord]:
        config = load_project_config(self._path)
        records: list[ClientRecord] = []
        for env_name, platform_env in self._iter_platform_environments(config, environment):
            for client_id, reporting_company, client_cfg in iter_portal_reporting_clients(platform_env):
                try:
                    company_env = get_environment_config(reporting_company, env_name, path=self._path)
                except KeyError:
                    continue
                connectors = list(iter_configured_connectors(company_env))
                connector_sources = tuple(source for source, _cfg in connectors)
                dna_cfg = company_env.get("dna", {})
                dna_enabled = bool(dna_cfg.get("enabled")) if isinstance(dna_cfg, dict) else False
                records.append(
                    ClientRecord(
                        company=_normalize_company(reporting_company),
                        client_id=client_id,
                        environment=env_name,
                        connector_sources=connector_sources,
                        portal_display_name=str(client_cfg.get("display_name", client_id)),
                        reporting_hostname=str(client_cfg.get("reporting_hostname", client_id)),
                        dna_enabled=dna_enabled,
                    )
                )
        return records

    def get_client(
        self,
        company: str,
        *,
        environment: str,
        client_id: str | None = None,
    ) -> ClientRecord | None:
        company_key = _normalize_company(company)
        normalized_client_id = _normalize_client_id(client_id) if client_id else None
        records = self.list_clients(environment=environment)
        for record in records:
            if record.company != company_key:
                continue
            if normalized_client_id is None or record.client_id == normalized_client_id:
                return record
        if normalized_client_id:
            for record in records:
                if record.client_id == normalized_client_id:
                    return record
        for record in records:
            if record.client_id == company_key:
                return record
        return None

    def create_client(self, spec: ClientCreateSpec) -> ClientRecord:
        validate_client_create_spec(spec, path=self._path)
        company = _normalize_company(spec.company)
        client_id = _normalize_client_id(spec.client_id)
        environment = spec.environment.strip().lower()

        config = load_project_config(self._path)
        companies = config.setdefault("companies", {})
        if not isinstance(companies, dict):
            raise ValueError("config.yaml companies section must be a mapping")
        company_cfg = companies.setdefault(company, {})
        if not isinstance(company_cfg, dict):
            raise ValueError(f"companies.{company} must be a mapping")
        environments = company_cfg.setdefault("environments", {})
        if not isinstance(environments, dict):
            raise ValueError(f"companies.{company}.environments must be a mapping")
        environments[environment] = build_company_environment_config(spec)

        platform = config.setdefault("platform", {})
        if not isinstance(platform, dict):
            raise ValueError("config.yaml platform section must be a mapping")
        platform_envs = platform.setdefault("environments", {})
        if not isinstance(platform_envs, dict):
            raise ValueError("platform.environments must be a mapping")
        platform_env = platform_envs.setdefault(environment, {})
        if not isinstance(platform_env, dict):
            raise ValueError(f"platform.environments.{environment} must be a mapping")
        ui_cfg = platform_env.setdefault("ui", {})
        if not isinstance(ui_cfg, dict):
            raise ValueError(f"platform.environments.{environment}.ui must be a mapping")
        portal_cfg = ui_cfg.setdefault("portal", {})
        if not isinstance(portal_cfg, dict):
            raise ValueError("platform.ui.portal must be a mapping")
        clients = portal_cfg.setdefault("clients", {})
        if not isinstance(clients, dict):
            raise ValueError("platform.ui.portal.clients must be a mapping")
        clients[client_id] = _portal_client_block(spec)

        _assert_portal_client_reporting_company(
            config, environment=environment, client_id=client_id
        )
        save_project_config(config, self._path)
        return ClientRecord(
            company=company,
            client_id=client_id,
            environment=environment,
            connector_sources=tuple(item.source.strip().lower() for item in spec.connectors),
            portal_display_name=spec.portal.display_name.strip(),
            reporting_hostname=spec.portal.reporting_hostname.strip().lower(),
            dna_enabled=spec.dna.enabled,
        )

    def update_client(self, spec: ClientCreateSpec) -> ClientRecord:
        company = _normalize_company(spec.company)
        client_id = _normalize_client_id(spec.client_id)
        environment = spec.environment.strip().lower()
        if self.get_client(company, environment=environment, client_id=client_id) is None:
            raise ValueError(f"Client {company}/{client_id} not found in config.yaml")

        connector_sources = [item.source.strip().lower() for item in spec.connectors]
        if not spec.connectors:
            raise ValueError("At least one connector is required")
        if len(connector_sources) != len(set(connector_sources)):
            raise ValueError("Duplicate connector sources are not allowed")
        if not spec.portal.display_name.strip():
            raise ValueError("portal.display_name is required")
        if spec.dna.enabled:
            dna_source = spec.dna.source.strip().lower()
            if dna_source not in connector_sources:
                raise ValueError("dna.source must match one of the configured connectors")

        config = load_project_config(self._path)
        companies = config.get("companies", {})
        if not isinstance(companies, dict):
            raise ValueError("config.yaml companies section must be a mapping")
        company_cfg = companies.get(company)
        if not isinstance(company_cfg, dict):
            raise ValueError(f"companies.{company} must be a mapping")
        environments = company_cfg.get("environments", {})
        if not isinstance(environments, dict):
            raise ValueError(f"companies.{company}.environments must be a mapping")
        environments[environment] = build_company_environment_config(spec)

        platform = config.get("platform", {})
        if not isinstance(platform, dict):
            raise ValueError("config.yaml platform section must be a mapping")
        platform_envs = platform.get("environments", {})
        if not isinstance(platform_envs, dict):
            raise ValueError("platform.environments must be a mapping")
        platform_env = platform_envs.get(environment)
        if not isinstance(platform_env, dict):
            raise ValueError(f"platform.environments.{environment} must be a mapping")
        ui_cfg = platform_env.get("ui", {})
        if not isinstance(ui_cfg, dict):
            raise ValueError(f"platform.environments.{environment}.ui must be a mapping")
        portal_cfg = ui_cfg.get("portal", {})
        if not isinstance(portal_cfg, dict):
            raise ValueError("platform.ui.portal must be a mapping")
        clients = portal_cfg.get("clients", {})
        if not isinstance(clients, dict) or client_id not in clients:
            raise ValueError(f"Portal client {client_id!r} not found in config.yaml")
        clients[client_id] = _portal_client_block(spec)

        _assert_portal_client_reporting_company(
            config, environment=environment, client_id=client_id
        )
        save_project_config(config, self._path)
        return ClientRecord(
            company=company,
            client_id=client_id,
            environment=environment,
            connector_sources=tuple(connector_sources),
            portal_display_name=spec.portal.display_name.strip(),
            reporting_hostname=spec.portal.reporting_hostname.strip().lower(),
            dna_enabled=spec.dna.enabled,
        )

    def expected_stack_names(self, record: ClientRecord) -> list[str]:
        # A client's own stacks are its data plane only. The reporting UI is the
        # shared PortalStack (deployed once per environment, not per client) and
        # DNS is the `*.{zone}` wildcard — neither is provisioned per onboard.
        stacks = [ingest_stack_name(record.company, record.environment, path=self._path)]
        try:
            env_config = get_environment_config(record.company, record.environment, path=self._path)
            if is_dna_stack_enabled(env_config):
                stacks.append(dna_stack_name(record.company, record.environment, path=self._path))
        except KeyError:
            pass
        return stacks

    def get_deploy_status(self, record: ClientRecord, *, region: str | None = None) -> DeployStatus:
        stacks = [
            describe_stack_status(stack_name, region=region)
            for stack_name in self.expected_stack_names(record)
        ]
        return DeployStatus(
            company=record.company,
            client_id=record.client_id,
            environment=record.environment,
            stacks=stacks,
        )

    def secret_name(self, record: ClientRecord, *, source: str | None = None) -> str:
        resolved_source = (source or record.connector_source).strip().lower()
        return resolve_qbo_secret_name(
            record.company,
            record.environment,
            source=resolved_source,
        )

    @staticmethod
    def _iter_platform_environments(
        config: dict[str, Any],
        environment: str | None,
    ):
        platform = config.get("platform", {})
        if not isinstance(platform, dict):
            return
        environments = platform.get("environments", {})
        if not isinstance(environments, dict):
            return
        for env_name, env_cfg in environments.items():
            if environment and env_name != environment:
                continue
            if isinstance(env_cfg, dict):
                yield env_name, env_cfg


def _map_stack_status(raw: str | None) -> StackLifecycle:
    value = (raw or "").strip().upper()
    if not value:
        return StackLifecycle.UNKNOWN
    if value in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "ROLLBACK_COMPLETE"}:
        return StackLifecycle.COMPLETE
    if value in {"CREATE_FAILED", "ROLLBACK_FAILED", "DELETE_FAILED", "UPDATE_ROLLBACK_FAILED"}:
        return StackLifecycle.FAILED
    if value.endswith("_IN_PROGRESS") or value in {"REVIEW_IN_PROGRESS"}:
        return StackLifecycle.IN_PROGRESS
    if value == "NOT_FOUND":
        return StackLifecycle.NOT_FOUND
    return StackLifecycle.UNKNOWN


def describe_stack_status(stack_name: str, *, region: str | None = None) -> StackDeployStatus:
    """Return CloudFormation stack status (or NOT_FOUND when absent)."""
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError as exc:
        raise RuntimeError("boto3 is required to query CloudFormation stack status") from exc

    client = boto3.client("cloudformation", region_name=region)
    try:
        response = client.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ValidationError" and "does not exist" in str(exc):
            return StackDeployStatus(stack_name=stack_name, status=StackLifecycle.NOT_FOUND)
        raise

    stacks = response.get("Stacks", [])
    if not stacks:
        return StackDeployStatus(stack_name=stack_name, status=StackLifecycle.NOT_FOUND)

    stack = stacks[0]
    lifecycle = _map_stack_status(str(stack.get("StackStatus", "")))
    reason = str(stack.get("StackStatusReason", "") or "")

    events: list[dict[str, str]] = []
    try:
        event_response = client.describe_stack_events(StackName=stack_name)
        for item in event_response.get("StackEvents", [])[:10]:
            events.append(
                {
                    "timestamp": str(item.get("Timestamp", "")),
                    "resource": str(item.get("LogicalResourceId", "")),
                    "status": str(item.get("ResourceStatus", "")),
                    "reason": str(item.get("ResourceStatusReason", "") or ""),
                }
            )
    except ClientError:
        pass

    return StackDeployStatus(
        stack_name=stack_name,
        status=lifecycle,
        status_reason=reason,
        events=events,
    )


def _build_phase_reason(build: dict[str, Any]) -> str:
    phase = str(build.get("current_phase", "")).strip().replace("_", " ").lower()
    if phase:
        return f"CodeBuild {phase}…"
    return "CodeBuild deploy in progress…"


def merge_stack_status_with_build(
    stacks: list[StackDeployStatus],
    build: dict[str, Any],
) -> list[StackDeployStatus]:
    """While CodeBuild is running, hide stale CloudFormation COMPLETE states."""
    build_status = str(build.get("status", "")).strip().upper()
    if build_status != "IN_PROGRESS":
        return stacks

    reason = _build_phase_reason(build)
    merged: list[StackDeployStatus] = []
    for item in stacks:
        if item.status in {StackLifecycle.IN_PROGRESS, StackLifecycle.FAILED}:
            merged.append(item)
            continue
        merged.append(
            StackDeployStatus(
                stack_name=item.stack_name,
                status=StackLifecycle.IN_PROGRESS,
                status_reason=reason,
                events=item.events,
            )
        )
    return merged


def verify_post_deploy(record: ClientRecord, *, region: str | None = None) -> dict[str, Any]:
    """Check S3 governance seed and bronze manifest after deploy."""
    from hiveflow.project_config import resolve_aws_deploy_env, resolve_data_bucket_name
    from hiveflow.storage.paths import company_dna_config_id, governance_pack_prefix

    checks: dict[str, Any] = {"company": record.company, "client_id": record.client_id}
    try:
        env_config = get_environment_config(record.company, record.environment)
        account, deploy_region = resolve_aws_deploy_env(env_config, record.environment)
        bucket = resolve_data_bucket_name(
            record.company,
            record.environment,
            account=account,
            region=deploy_region,
        )
        checks["bucket"] = bucket
        pack_id = company_dna_config_id(record.company)
        prefix = governance_pack_prefix(pack_id)
        checks["governance_prefix"] = prefix

        import boto3

        s3 = boto3.client("s3", region_name=region or deploy_region)
        workflow_key = f"{prefix}/workflow.json"
        try:
            s3.head_object(Bucket=bucket, Key=workflow_key)
            checks["governance_seeded"] = True
        except Exception:
            checks["governance_seeded"] = False

        source = record.connector_source or "dbc"
        manifest_prefix = f"raw/{source}/"
        response = s3.list_objects_v2(Bucket=bucket, Prefix=manifest_prefix, MaxKeys=1)
        checks["bronze_present"] = bool(response.get("Contents"))
    except Exception as exc:
        checks["error"] = str(exc)
    return checks
