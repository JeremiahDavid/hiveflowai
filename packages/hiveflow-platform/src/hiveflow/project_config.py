from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

from hiveflow.repo_paths import find_project_root

PROJECT_ROOT = find_project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
LAMBDA_WRITABLE_CONFIG_PATH = Path("/tmp/hiveflow/config.yaml")
PROTECTED_ENVIRONMENTS = frozenset({"prod"})
PLACEHOLDER_ACCOUNT_PREFIXES = ("REPLACE", "CHANGEME", "YOUR_")


def cost_allocation_tags(
    company: str,
    environment: str,
    *,
    application: str = "hiveflow",
) -> dict[str, str]:
    """Standard AWS resource tags for cost and billing attribution."""
    company_slug = company.strip().lower()
    environment_slug = environment.strip().lower()
    if not company_slug:
        raise ValueError("company is required for cost allocation tags")
    if not environment_slug:
        raise ValueError("environment is required for cost allocation tags")

    return {
        "Company": company_slug,
        "Environment": environment_slug,
        "Application": application.strip() or "hiveflow",
    }


def aws_tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    """Convert a tag mapping to the list format expected by boto3 APIs."""
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def default_config_path() -> Path:
    configured = os.getenv("HIVEFLOW_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured)

    return find_project_root() / "config.yaml"


def is_lambda_runtime() -> bool:
    return bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME", "").strip())


def config_s3_uri() -> str:
    return os.getenv("HIVEFLOW_CONFIG_S3_URI", "").strip()


def bundled_config_path() -> Path:
    return find_project_root() / "config.yaml"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    normalized = uri.strip()
    if not normalized.startswith("s3://"):
        raise ValueError(f"Expected s3:// URI, got {uri!r}")
    bucket, _, key = normalized[5:].partition("/")
    if not bucket or not key:
        raise ValueError(f"S3 URI must include an object key: {uri!r}")
    return bucket, key


def download_config_from_s3(uri: str, dest: Path) -> bool:
    import boto3
    from botocore.exceptions import ClientError

    bucket, key = parse_s3_uri(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        boto3.client("s3").download_file(bucket, key, str(dest))
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return dest.is_file()


def upload_config_to_s3(uri: str, source: Path) -> None:
    import boto3

    bucket, key = parse_s3_uri(uri)
    boto3.client("s3").upload_file(str(source), bucket, key)


def sync_config_to_s3(source: Path, *, uri: str | None = None) -> None:
    resolved = (uri or config_s3_uri()).strip()
    if resolved:
        upload_config_to_s3(resolved, source)


def refresh_platform_config() -> Path:
    """Re-download platform config.yaml from S3 when running against a remote store."""
    s3_uri = config_s3_uri()
    if not s3_uri:
        return default_config_path()

    if is_lambda_runtime():
        dest = Path(os.getenv("HIVEFLOW_CONFIG_PATH", "")).expanduser() if os.getenv("HIVEFLOW_CONFIG_PATH") else LAMBDA_WRITABLE_CONFIG_PATH
    else:
        dest = default_config_path()

    dest.parent.mkdir(parents=True, exist_ok=True)
    if download_config_from_s3(s3_uri, dest):
        os.environ["HIVEFLOW_CONFIG_PATH"] = str(dest)
        load_project_config.cache_clear()
    return dest


def ensure_writable_config_path() -> Path:
    """Use a writable config.yaml copy in Lambda (/var/task is read-only)."""
    configured = os.getenv("HIVEFLOW_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured)

    bundled = bundled_config_path()
    if not is_lambda_runtime():
        return bundled

    writable = LAMBDA_WRITABLE_CONFIG_PATH
    if writable.is_file():
        os.environ["HIVEFLOW_CONFIG_PATH"] = str(writable)
        load_project_config.cache_clear()
        return writable

    writable.parent.mkdir(parents=True, exist_ok=True)
    s3_uri = config_s3_uri()
    if s3_uri and download_config_from_s3(s3_uri, writable):
        pass
    elif bundled.is_file():
        shutil.copy2(bundled, writable)
        if s3_uri:
            upload_config_to_s3(s3_uri, writable)
    else:
        writable.write_text("{}\n", encoding="utf-8")

    os.environ["HIVEFLOW_CONFIG_PATH"] = str(writable)
    load_project_config.cache_clear()
    return writable


def sync_config_for_codebuild() -> Path:
    """Download the platform config.yaml from S3 into the repo root for CDK deploy."""
    s3_uri = config_s3_uri()
    dest = find_project_root() / "config.yaml"
    if not s3_uri:
        return dest
    if not download_config_from_s3(s3_uri, dest):
        raise FileNotFoundError(
            f"Platform config not found at {s3_uri}. Save client config in the admin wizard first."
        )
    load_project_config.cache_clear()
    return dest


@lru_cache(maxsize=1)
def load_project_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or default_config_path()
    if not config_path.exists():
        return {}

    with config_path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)

    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping at the top level")
    return payload


def save_project_config(config: dict[str, Any], path: Path | None = None) -> Path:
    """Write config.yaml and clear the in-process config cache."""
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")

    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            config,
            handle,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
    load_project_config.cache_clear()
    sync_config_to_s3(config_path)
    return config_path


def _available_companies(config: dict[str, Any]) -> list[str]:
    companies = config.get("companies", {})
    if not isinstance(companies, dict):
        return []
    return sorted(companies)


def _resolve_company_key(config: dict[str, Any], company: str) -> str:
    companies = config.get("companies", {})
    if not isinstance(companies, dict):
        raise KeyError(f"Unknown company {company!r}")

    normalized = company.strip().lower()
    if normalized in companies:
        return normalized

    for key in companies:
        if str(key).strip().lower() == normalized:
            return str(key)

    available = ", ".join(sorted(companies)) or "(none)"
    raise KeyError(f"Unknown company {company!r}. Available companies: {available}")


def _available_environments(config: dict[str, Any], company: str) -> list[str]:
    company_cfg = config.get("companies", {}).get(company, {})
    environments = company_cfg.get("environments", {})
    if not isinstance(environments, dict):
        return []
    return sorted(environments)


def resolve_selection(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> tuple[str, str]:
    config = load_project_config(path)
    defaults = config.get("default", {})
    if not isinstance(defaults, dict):
        defaults = {}

    selected_company = (
        company
        or os.getenv("HIVEFLOW_COMPANY", "").strip()
        or str(defaults.get("company", "")).strip()
    )
    selected_environment = (
        environment
        or os.getenv("HIVEFLOW_ENVIRONMENT", "").strip()
        or str(defaults.get("environment", "")).strip()
    )

    if not selected_company or not selected_environment:
        raise ValueError(
            "Company and environment must be set via args, HIVEFLOW_COMPANY/HIVEFLOW_ENVIRONMENT, "
            "or default.company/default.environment in config.yaml"
        )

    selected_company = _resolve_company_key(config, selected_company)

    environments = _available_environments(config, selected_company)
    if selected_environment not in environments:
        raise KeyError(
            f"Unknown environment {selected_environment!r} for company {selected_company!r}. "
            f"Available environments: {', '.join(environments) or '(none)'}"
        )

    return selected_company, selected_environment


def get_environment_config(
    company: str,
    environment: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    config = load_project_config(path)
    company_key = _resolve_company_key(config, company)
    env_config = config["companies"][company_key]["environments"][environment]
    if not isinstance(env_config, dict):
        raise ValueError(
            f"Environment config for {company}/{environment} must be a mapping"
        )
    return env_config


def get_config_value(
    key_path: str,
    default: Any = None,
    *,
    company: str | None = None,
    environment: str | None = None,
    path: Path | None = None,
) -> Any:
    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    value: Any = get_environment_config(selected_company, selected_environment, path=path)
    for part in key_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def iter_deploy_targets(
    *,
    company: str | None = None,
    environment: str | None = None,
    path: Path | None = None,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    config = load_project_config(path)

    if company or environment:
        selected_company, selected_environment = resolve_selection(
            company,
            environment,
            path=path,
        )
        yield (
            selected_company,
            selected_environment,
            get_environment_config(selected_company, selected_environment, path=path),
        )
        return

    for company_name in _available_companies(config):
        for environment_name in _available_environments(config, company_name):
            yield (
                company_name,
                environment_name,
                get_environment_config(company_name, environment_name, path=path),
            )


def stack_name_company_slug(company: str, *, path: Path | None = None) -> str:
    """Slug embedded in CloudFormation stack names.

    Legacy tenants may set ``companies.<id>.stack_name_company`` to keep an
    already-deployed stack name (e.g. ``POC``). New companies default to the
    lowercase config key.
    """
    config = load_project_config(path)
    company_key = _resolve_company_key(config, company)
    company_cfg = config["companies"].get(company_key, {})
    if isinstance(company_cfg, dict):
        override = str(company_cfg.get("stack_name_company", "")).strip()
        if override:
            return override
    return company_key.strip().lower()


def ingest_stack_name(company: str, environment: str, *, path: Path | None = None) -> str:
    slug = stack_name_company_slug(company, path=path)
    return f"IngestStack-{slug}-{environment.strip().lower()}"


def dna_stack_name(company: str, environment: str, *, path: Path | None = None) -> str:
    slug = stack_name_company_slug(company, path=path)
    return f"DnaStack-{slug}-{environment.strip().lower()}"


def ui_stack_name(company: str, environment: str, *, path: Path | None = None) -> str:
    slug = stack_name_company_slug(company, path=path)
    return f"UiStack-{slug}-{environment.strip().lower()}"


def global_ui_stack_name(environment: str) -> str:
    return f"GlobalUiStack-{environment}"


def global_dns_stack_name(environment: str) -> str:
    return f"GlobalDnsStack-{environment}"


def global_ui_web_api_export_name(environment: str) -> str:
    return f"hiveflow-global-ui-{environment}-web-api-id"


def reporting_web_api_export_name(client_id: str, environment: str) -> str:
    slug = client_id.strip().lower().replace("_", "-")
    return f"hiveflow-reporting-{slug}-{environment}-web-api-id"


def reporting_stack_name(client_id: str, environment: str) -> str:
    slug = client_id.strip().lower().replace("_", "-")
    return f"ReportingStack-{slug}-{environment.strip().lower()}"


def portal_stack_name(environment: str) -> str:
    """The single multi-tenant reporting portal stack (replaces per-client ReportingStack)."""
    return f"PortalStack-{environment.strip().lower()}"


def portal_stack_module_name() -> str:
    return "portal_stack"


def portal_web_api_export_name(environment: str) -> str:
    return f"hiveflow-portal-{environment.strip().lower()}-web-api-id"


def deploy_stack_names(
    *,
    company: str,
    environment: str,
    client_id: str | None = None,
    scope: str = "all",
    path: Path | None = None,
) -> list[str]:
    """Resolve CloudFormation stack names for scoped client provisioning."""
    names: list[str] = []
    scope_normalized = scope.strip().lower() or "all"
    if scope_normalized in ("all", "ingest"):
        names.extend(
            [
                ingest_stack_name(company, environment, path=path),
                dna_stack_name(company, environment, path=path),
            ]
        )
    # Onboarding a client no longer deploys a per-client ReportingStack or touches
    # DNS — the shared PortalStack + `*.{zone}` wildcard already serve every
    # client. PortalStack is deployed once at platform scope by the operator.
    return names


def reporting_stack_module_name() -> str:
    return "reporting_stack"


def global_ui_stack_module_name() -> str:
    return "global_ui_stack"


def global_dns_stack_module_name() -> str:
    return "global_dns_stack"


def global_dna_stack_name(environment: str) -> str:
    return f"GlobalDnaStack-{environment}"


def global_dna_stack_module_name() -> str:
    return "global_dna_stack"


def global_agent_pipelines_stack_name(environment: str) -> str:
    return f"GlobalAgentPipelinesStack-{environment}"


def global_agent_pipelines_stack_module_name() -> str:
    return "global_agent_pipelines_stack"


def portal_session_secret_name(environment: str) -> str:
    """Pinned Secrets Manager name for the global portal's session-signing secret.

    Shared by ``GlobalUiStack`` (which creates it) and any stack that only
    needs to read it (e.g. ``GlobalAgentPipelinesStack``, which imports it by
    this name via ``Secret.from_secret_name_v2`` instead of taking a live
    construct reference — that's what lets it deploy without also
    constructing ``GlobalUiStack`` and paying for its Lambda bundling).
    """
    return f"meshflow-platform-portal-session-{environment.strip().lower()}"


def spreadsheet_engine_state_machine_name(environment: str) -> str:
    """Name of the shared, global Spreadsheet Engine Step Functions state machine.

    One deployment per environment (``GlobalAgentPipelinesStack``) serves every
    company — invoked with ``company`` in the execution input rather than one
    state machine per company.
    """
    return f"platform-{environment.strip().lower()}-spreadsheet"


def agent_pipelines_role_name(environment: str) -> str:
    """Shared IAM execution role for every Lambda in ``GlobalAgentPipelinesStack``.

    A single, predictably-named role (rather than one auto-generated role per
    function) is what lets each company's tenant role
    (``hiveflow-portal-tenant-{company}-{environment}``) trust every global
    agent-pipeline Lambda with one trust-policy entry.
    """
    return f"hiveflow-agent-pipelines-{environment.strip().lower()}-role"


def platform_admin_stack_name(environment: str) -> str:
    return f"PlatformAdminStack-{environment}"


def platform_admin_stack_module_name() -> str:
    return "platform_admin_stack"


def platform_admin_web_api_export_name(environment: str) -> str:
    return f"hiveflow-platform-admin-{environment}-web-api-id"


def provisioning_stack_name(environment: str) -> str:
    return f"ProvisioningStack-{environment}"


def provisioning_stack_module_name() -> str:
    return "provisioning_stack"


def get_spreadsheet_engine_hostname(ui_config: dict[str, Any]) -> str:
    """Subdomain for the Spreadsheet Engine UI, from
    ``platform.environments.<env>.ui.spreadsheet_engine.hostname``."""
    cfg = ui_config.get("spreadsheet_engine", {})
    if not isinstance(cfg, dict):
        cfg = {}
    return str(cfg.get("hostname", "spreadsheet-engine")).strip().lower() or "spreadsheet-engine"


def get_platform_config(*, path: Path | None = None) -> dict[str, Any]:
    config = load_project_config(path)
    platform_cfg = config.get("platform", {})
    if not isinstance(platform_cfg, dict):
        return {}
    return platform_cfg


def _available_platform_environments(config: dict[str, Any]) -> list[str]:
    platform_cfg = config.get("platform", {})
    if not isinstance(platform_cfg, dict):
        return []
    environments = platform_cfg.get("environments", {})
    if not isinstance(environments, dict):
        return []
    return sorted(environments)


def get_platform_environment_config(
    environment: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    config = load_project_config(path)
    platform_cfg = config.get("platform", {})
    if not isinstance(platform_cfg, dict):
        raise KeyError("platform section is missing from config.yaml")

    environments = platform_cfg.get("environments", {})
    if not isinstance(environments, dict):
        raise KeyError("platform.environments is missing from config.yaml")

    env_config = environments.get(environment)
    if not isinstance(env_config, dict):
        available = ", ".join(_available_platform_environments(config)) or "(none)"
        raise KeyError(
            f"Unknown platform environment {environment!r}. Available: {available}"
        )
    return env_config


def iter_platform_deploy_environments(*, path: Path | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    config = load_project_config(path)
    for environment_name in _available_platform_environments(config):
        yield environment_name, get_platform_environment_config(environment_name, path=path)


def is_platform_ui_enabled(platform_env_config: dict[str, Any]) -> bool:
    ui_cfg = get_ui_config(platform_env_config)
    return bool(ui_cfg.get("enabled", False))


def iter_portal_reporting_clients(
    platform_env_config: dict[str, Any],
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (client_id, reporting_company, client_cfg) for portal clients with a reporting backend."""
    ui_cfg = get_ui_config(platform_env_config)
    portal_cfg = ui_cfg.get("portal", {})
    if not isinstance(portal_cfg, dict):
        return

    clients = portal_cfg.get("clients", {})
    if not isinstance(clients, dict):
        return

    for client_id, raw in clients.items():
        if not isinstance(raw, dict):
            continue
        reporting_company = str(raw.get("reporting_company", "")).strip()
        if reporting_company:
            yield str(client_id).strip().lower(), reporting_company, raw


def resolve_portal_client_buckets(
    platform_env_config: dict[str, Any],
    environment: str,
    *,
    account: str,
    region: str,
    path: Path | None = None,
) -> dict[str, str]:
    """Map portal client_id → S3 data bucket for Global UI Lambda grants."""
    buckets: dict[str, str] = {}
    for client_id, reporting_company, _raw in iter_portal_reporting_clients(platform_env_config):
        bucket = resolve_data_bucket_name(
            reporting_company,
            environment,
            account=account,
            region=region,
            path=path,
        )
        buckets[client_id] = bucket
    return buckets


def ingest_stack_module_name(company: str | None = None) -> str:
    """Python module name for the generic company ingest stack."""
    del company  # company is passed as a construct prop, not via module name
    return "ingest_stack"


def dna_stack_module_name(company: str | None = None) -> str:
    """Python module name for the generic company DNA stack."""
    del company  # company is passed as a construct prop, not via module name
    return "dna_stack"


def ui_stack_module_name(company: str) -> str:
    """Python module name for a company UI stack file."""
    return f"uistack_{company.strip().lower()}"


def get_dna_config(env_config: dict[str, Any]) -> dict[str, Any]:
    """Return DNA stack settings from config.yaml (empty if disabled)."""
    dna_cfg = env_config.get("dna", {})
    if not isinstance(dna_cfg, dict):
        return {}
    return dna_cfg


def is_dna_stack_enabled(env_config: dict[str, Any]) -> bool:
    """True when the independent DNA stack should be synthesized."""
    dna_cfg = get_dna_config(env_config)
    return bool(dna_cfg.get("enabled", False))


def get_ui_config(env_config: dict[str, Any]) -> dict[str, Any]:
    """Return UI stack settings from config.yaml (empty if disabled)."""
    ui_cfg = env_config.get("ui", {})
    if not isinstance(ui_cfg, dict):
        return {}
    return ui_cfg


def get_ui_domain_config(env_config: dict[str, Any]) -> dict[str, Any]:
    """Return custom domain settings for the HiveFlowAI UI (empty if not configured)."""
    ui_cfg = get_ui_config(env_config)
    domain_cfg = ui_cfg.get("domain", {})
    if not isinstance(domain_cfg, dict):
        return {}
    return domain_cfg


def is_ui_dns_managed(env_config: dict[str, Any]) -> bool:
    """True only for GlobalDnsStack bootstrap (Route53 records, ACM, custom domain mappings).

    Routine GlobalUiStack deploys should leave this false so website updates do not
    touch DNS or hosted zones.
    """
    domain_cfg = get_ui_domain_config(env_config)
    if not domain_cfg:
        return False
    if "manage_dns" in domain_cfg:
        return bool(domain_cfg.get("manage_dns"))
    hosted_zone_id = str(domain_cfg.get("hosted_zone_id", "")).strip()
    if hosted_zone_id:
        return False
    return bool(domain_cfg.get("create_hosted_zone", False))


def is_global_dns_stack_enabled(env_config: dict[str, Any]) -> bool:
    """True when GlobalDnsStack is part of the CDK app (after bootstrap or during DNS setup)."""
    domain_cfg = get_ui_domain_config(env_config)
    zone_name = str(domain_cfg.get("zone_name", "")).strip()
    if not zone_name:
        return False
    if is_ui_dns_managed(env_config):
        return True
    return bool(str(domain_cfg.get("hosted_zone_id", "")).strip())


def resolve_ui_primary_site_url(env_config: dict[str, Any], *, fallback: str = "") -> str:
    """Public site URL from config (used when DNS is not managed by GlobalUiStack)."""
    domain_cfg = get_ui_domain_config(env_config)
    zone_name = str(domain_cfg.get("zone_name", "")).strip().lower().rstrip(".")
    primary_hostname = str(domain_cfg.get("primary_hostname", zone_name)).strip().lower().rstrip(".")
    if primary_hostname:
        return f"https://{primary_hostname}/"
    return fallback


def resolve_reporting_site_url(
    domain_config: dict[str, Any],
    client_config: dict[str, Any],
    client_id: str,
    *,
    fallback: str = "",
) -> str:
    """Client reporting URL from config (GlobalDnsStack maps the hostname when manage_dns is true)."""
    zone_name = str(domain_config.get("zone_name", "")).strip().lower().rstrip(".")
    if not zone_name:
        return fallback
    reporting_hostname = str(client_config.get("reporting_hostname", client_id)).strip().lower().rstrip(".")
    if reporting_hostname.endswith(zone_name):
        hostname = reporting_hostname
    else:
        hostname = f"{reporting_hostname}.{zone_name}"
    return f"https://{hostname}/"


def is_ui_domain_enabled(env_config: dict[str, Any]) -> bool:
    """True when a public hostname is configured (for cookies and redirects)."""
    domain_cfg = get_ui_domain_config(env_config)
    return bool(str(domain_cfg.get("zone_name", "")).strip())


def resolve_dna_source(env_config: dict[str, Any]) -> str:
    """Primary silver source connector for DNA (defaults to dbc when configured)."""
    dna_cfg = get_dna_config(env_config)
    explicit = str(dna_cfg.get("source", "")).strip().lower()
    if explicit:
        return explicit
    connectors = [name for name, _cfg in iter_configured_connectors(env_config)]
    if "dbc" in connectors:
        return "dbc"
    if connectors:
        return connectors[0]
    return "dbc"


CONNECTOR_NAMES = ("qbo", "qbd", "dbc")


def normalize_connector(connector: str) -> str:
    """Normalize connector slug; legacy ``bc`` maps to ``dbc``."""
    normalized = connector.strip().lower()
    if normalized == "bc":
        return "dbc"
    return normalized


def is_dbc_connector(connector: str) -> bool:
    return normalize_connector(connector) == "dbc"


def is_bc_connector(connector: str) -> bool:
    """Deprecated alias for :func:`is_dbc_connector`."""
    return is_dbc_connector(connector)


def get_connector_config(
    env_config: dict[str, Any],
    connector: str,
) -> dict[str, Any]:
    """Return per-connector settings from config.yaml."""
    connector = connector.strip().lower()
    cfg = env_config.get(connector, {})
    if isinstance(cfg, dict) and cfg:
        return cfg

    ingest_cfg = env_config.get("ingest", {})
    if isinstance(ingest_cfg, dict) and str(ingest_cfg.get("connector", "")).strip().lower() == connector:
        return ingest_cfg
    return {}


def iter_configured_connectors(
    env_config: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (connector, config) for each connector block under an environment."""
    found = False
    for connector in CONNECTOR_NAMES:
        cfg = env_config.get(connector, {})
        if isinstance(cfg, dict) and cfg:
            found = True
            yield connector, cfg

    if found:
        return

    ingest_cfg = env_config.get("ingest", {})
    if isinstance(ingest_cfg, dict):
        connector = str(ingest_cfg.get("connector", "")).strip().lower()
        if connector:
            yield connector, ingest_cfg


def resolve_qbo_secret_name(
    company: str | None = None,
    environment: str | None = None,
    source: str | None = None,
    *,
    path: Path | None = None,
) -> str:
    """Derive the Secrets Manager name from config.yaml company/source/environment."""
    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    config = load_project_config(path)
    secrets_cfg = config.get("secrets", {})
    if not isinstance(secrets_cfg, dict):
        secrets_cfg = {}

    template = str(
        secrets_cfg.get(
            "secret_name_template",
            secrets_cfg.get("qbo_name_template", "hiveflow-{company}-{source}-{environment}"),
        )
    ).strip()
    env_config = get_environment_config(selected_company, selected_environment, path=path)

    resolved_source = (source or os.getenv("HIVEFLOW_SOURCE", "")).strip()
    if not resolved_source:
        connectors = list(iter_configured_connectors(env_config))
        if len(connectors) == 1:
            resolved_source = connectors[0][0]
        elif isinstance(env_config.get("ingest"), dict):
            resolved_source = str(env_config["ingest"].get("connector", "")).strip()
        if not resolved_source:
            dna_cfg = env_config.get("dna", {})
            if isinstance(dna_cfg, dict):
                resolved_source = str(dna_cfg.get("source", "")).strip()
    if not resolved_source:
        raise ValueError(
            f"Could not resolve secret source for {selected_company}/{selected_environment}. "
            "Set source in the secrets YAML, HIVEFLOW_SOURCE, or ingest.connector in config.yaml."
        )

    company_slug = selected_company.strip().lower()
    source_slug = resolved_source.strip().lower()
    environment_slug = selected_environment.strip().lower()

    return template.format(
        company=company_slug,
        source=source_slug,
        environment=environment_slug,
        tier=environment_slug,
    )


def resolve_data_bucket_name(
    company: str | None = None,
    environment: str | None = None,
    *,
    account: str | None = None,
    region: str | None = None,
    path: Path | None = None,
) -> str:
    """Derive the shared company data lake S3 bucket name from config.yaml."""
    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    config = load_project_config(path)
    secrets_cfg = config.get("secrets", {})
    if not isinstance(secrets_cfg, dict):
        secrets_cfg = {}

    env_config = get_environment_config(selected_company, selected_environment, path=path)

    resolved_account, resolved_region = resolve_aws_deploy_env(env_config, selected_environment)
    if account:
        resolved_account = account.strip()
    if region:
        resolved_region = region.strip()

    resolved_account = str(resolved_account or "").strip()
    resolved_region = str(resolved_region or "").strip()
    if not resolved_account:
        raise ValueError(
            "Could not resolve AWS account ID for bucket naming. "
            "Set CDK_DEFAULT_ACCOUNT or companies.*.environments.*.aws.account in config.yaml."
        )
    if not resolved_region:
        raise ValueError(
            "Could not resolve AWS region for bucket naming. "
            "Set CDK_DEFAULT_REGION or companies.*.environments.*.aws.region in config.yaml."
        )

    template = str(
        secrets_cfg.get(
            "data_bucket_name_template",
            secrets_cfg.get(
                "raw_bucket_name_template",
                "hiveflow-{company}-{account}-{region}",
            ),
        )
    ).strip()
    company_slug = selected_company.strip().lower()
    environment_slug = selected_environment.strip().lower()

    return template.format(
        company=company_slug,
        environment=environment_slug,
        account=resolved_account,
        region=resolved_region.lower(),
        tier=str(env_config.get("qbo", {}).get("tier", environment_slug)).strip().lower()
        if isinstance(env_config.get("qbo"), dict)
        else environment_slug,
    )


def resolve_raw_bucket_name(
    company: str | None = None,
    environment: str | None = None,
    *,
    account: str | None = None,
    region: str | None = None,
    path: Path | None = None,
) -> str:
    """Backward-compatible alias for resolve_data_bucket_name."""
    return resolve_data_bucket_name(
        company,
        environment,
        account=account,
        region=region,
        path=path,
    )


def resolve_ingest_s3_prefix(
    company: str | None = None,
    environment: str | None = None,
    *,
    source: str | None = None,
    path: Path | None = None,
) -> str:
    """Derive the raw-layer prefix for a connector (e.g. raw/qbd)."""
    from hiveflow.storage.paths import raw_source_prefix

    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    env_config = get_environment_config(selected_company, selected_environment, path=path)

    resolved_source = (source or os.getenv("HIVEFLOW_SOURCE", "")).strip().lower()
    if not resolved_source:
        connectors = list(iter_configured_connectors(env_config))
        if len(connectors) == 1:
            resolved_source = connectors[0][0]

    connector_cfg = get_connector_config(env_config, resolved_source) if resolved_source else {}
    explicit_prefix = str(connector_cfg.get("s3_prefix", "")).strip().strip("/")
    if explicit_prefix:
        return explicit_prefix

    if not resolved_source:
        ingest_cfg = env_config.get("ingest", {})
        if isinstance(ingest_cfg, dict):
            resolved_source = str(ingest_cfg.get("connector", "qbo")).strip().lower()

    if not resolved_source:
        raise ValueError(
            f"Set a connector block (qbo/qbd) or HIVEFLOW_SOURCE for "
            f"{selected_company}/{selected_environment} in config.yaml"
        )

    return raw_source_prefix(resolved_source)


def glue_database_name(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> str:
    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    config = load_project_config(path)
    secrets_cfg = config.get("secrets", {})
    if not isinstance(secrets_cfg, dict):
        secrets_cfg = {}

    template = str(
        secrets_cfg.get("glue_database_name_template", "hiveflow_{company}_{environment}")
    ).strip()
    return template.format(
        company=selected_company,
        environment=selected_environment,
    ).lower()


def athena_workgroup_name(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> str:
    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    return f"hiveflow-{selected_company}-{selected_environment}".lower()


def hiveflow_resource_name(
    company: str,
    environment: str,
    connector: str,
    stage: str,
    process: str,
    *,
    max_length: int = 64,
) -> str:
    """Build ``{company}-{environment}-{connector}-{stage}-{process}`` AWS resource names."""
    parts = [
        company.strip().lower(),
        environment.strip().lower(),
        connector.strip().lower(),
        stage.strip().lower(),
        process.strip().lower(),
    ]
    name = "-".join(part for part in parts if part)
    if not name:
        raise ValueError("Resource name requires company, environment, connector, stage, and process")
    if len(name) > max_length:
        raise ValueError(f"Resource name exceeds {max_length} characters: {name!r}")
    return name


def lambda_function_name(
    company: str,
    environment: str,
    connector: str,
    stage: str,
    process: str,
) -> str:
    """AWS Lambda function name."""
    return hiveflow_resource_name(company, environment, connector, stage, process, max_length=64)


def step_function_name(
    company: str,
    environment: str,
    connector: str,
    stage: str,
    process: str,
) -> str:
    """AWS Step Functions state machine name."""
    return hiveflow_resource_name(company, environment, connector, stage, process, max_length=80)


def eventbridge_rule_name(
    company: str,
    environment: str,
    connector: str,
) -> str:
    """AWS EventBridge rule name: ``{company}-{environment}-{connector}``."""
    parts = [
        company.strip().lower(),
        environment.strip().lower(),
        connector.strip().lower(),
    ]
    name = "-".join(part for part in parts if part)
    if len(parts) != 3 or not name:
        raise ValueError("EventBridge rule name requires company, environment, and connector")
    if len(name) > 64:
        raise ValueError(f"EventBridge rule name exceeds 64 characters: {name!r}")
    return name


def catalog_table_name(layer: str, source: str, entity: str) -> str:
    return f"{layer.strip().lower()}_{source.strip().lower()}_{entity.strip().lower()}"


def dna_catalog_table_name(output_id: str) -> str:
    """Glue table name for DNA gold outputs (no source prefix)."""
    slug = output_id.strip().lower().replace("-", "_")
    return f"dna_{slug}"


SILVER_ONLY_CATALOG_ENTITIES: dict[str, frozenset[str]] = {
    "qbd": frozenset({"invoice_lines"}),
    "dbc": frozenset(
        {
            "sales_quote_lines",
            "sales_order_lines",
            "sales_shipment_lines",
            "sales_invoice_lines",
            "sales_credit_memo_lines",
            "purchase_order_lines",
            "purchase_receipt_lines",
            "purchase_invoice_lines",
            "purchase_credit_memo_lines",
        }
    ),
}


def is_silver_only_catalog_entity(source: str, entity: str) -> bool:
    return entity.strip().lower() in SILVER_ONLY_CATALOG_ENTITIES.get(source.strip().lower(), frozenset())


def iter_catalog_entities(
    connectors: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, str]]:
    """Return (source, entity) pairs configured for Glue/Athena tables."""
    from hiveflow.entity_registry import catalog_entity_names

    entities: list[tuple[str, str]] = []
    for connector, connector_cfg in connectors:
        if connector == "qbo":
            source = "qbo"
        elif connector == "qbd":
            source = "qbd"
        elif is_dbc_connector(connector):
            source = "dbc"
        else:
            continue
        names = catalog_entity_names(source, connector_cfg)
        entities.extend((source, name) for name in names)
    return entities


def iter_raw_catalog_entities(
    connectors: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, str]]:
    return [
        (source, entity)
        for source, entity in iter_catalog_entities(connectors)
        if not is_silver_only_catalog_entity(source, entity)
    ]


def iter_dna_catalog_outputs(pack_outputs: list[str] | None = None) -> list[str]:
    """Return DNA gold output IDs registered in Glue."""
    if pack_outputs:
        return [output_id.strip().lower() for output_id in pack_outputs if output_id.strip()]
    return [
        "out_dim_customers",
        "out_dim_items",
        "out_fact_revenue_lines",
        "out_kpi_snapshot",
        "out_rev_by_customer_period",
    ]


def resolve_athena_results_bucket_name(
    company: str | None = None,
    environment: str | None = None,
    *,
    account: str | None = None,
    region: str | None = None,
    path: Path | None = None,
) -> str:
    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    config = load_project_config(path)
    secrets_cfg = config.get("secrets", {})
    if not isinstance(secrets_cfg, dict):
        secrets_cfg = {}

    env_config = get_environment_config(selected_company, selected_environment, path=path)
    resolved_account, resolved_region = resolve_aws_deploy_env(env_config, selected_environment)
    if account:
        resolved_account = account.strip()
    if region:
        resolved_region = region.strip()

    resolved_account = str(resolved_account or "").strip()
    resolved_region = str(resolved_region or "").strip()
    if not resolved_account or not resolved_region:
        raise ValueError(
            "Could not resolve AWS account/region for Athena results bucket naming."
        )

    template = str(
        secrets_cfg.get(
            "athena_results_bucket_name_template",
            "athena-results-{company}-{account}-{region}",
        )
    ).strip()
    return template.format(
        company=selected_company.strip().lower(),
        environment=selected_environment.strip().lower(),
        account=resolved_account,
        region=resolved_region.lower(),
    )


def resolve_qbo_ingest_entities(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> tuple[str, dict[str, str]]:
    """Resolve QBO entity bundle and queries from config.yaml ingest settings."""
    from hiveflow.entity_registry import resolve_bundle

    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    env_config = get_environment_config(selected_company, selected_environment, path=path)
    qbo_cfg = get_connector_config(env_config, "qbo")
    if not qbo_cfg:
        ingest_cfg = env_config.get("ingest", {})
        qbo_cfg = ingest_cfg if isinstance(ingest_cfg, dict) else {}

    return resolve_bundle("qbo", qbo_cfg)


def resolve_qbd_ingest_entities(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> tuple[str, list[Any]]:
    """Resolve QBD entity bundle and export specs from config.yaml ingest settings."""
    from hiveflow.entity_registry import resolve_bundle

    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    env_config = get_environment_config(selected_company, selected_environment, path=path)
    qbd_cfg = get_connector_config(env_config, "qbd")
    if not qbd_cfg:
        ingest_cfg = env_config.get("ingest", {})
        qbd_cfg = ingest_cfg if isinstance(ingest_cfg, dict) else {}

    return resolve_bundle("qbd", qbd_cfg)


def resolve_bc_ingest_entities(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> tuple[str, list[Any]]:
    """Resolve BC entity bundle and OData specs from config.yaml ingest settings."""
    from hiveflow.entity_registry import resolve_bundle

    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    env_config = get_environment_config(selected_company, selected_environment, path=path)
    dbc_cfg = get_connector_config(env_config, "dbc")
    if not dbc_cfg:
        ingest_cfg = env_config.get("ingest", {})
        dbc_cfg = ingest_cfg if isinstance(ingest_cfg, dict) else {}

    return resolve_bundle("dbc", dbc_cfg)


def resolve_fanout_entity_names(
    connector: str,
    connector_cfg: dict[str, Any],
) -> list[str]:
    """Return API entity names for scheduled bronze Glue ingest."""
    from hiveflow.entity_registry import fanout_entity_names

    normalized = normalize_connector(connector)
    if normalized not in {"qbo", "qbd", "dbc"}:
        raise ValueError(f"Unsupported connector for fan-out ingest: {connector!r}")
    return fanout_entity_names(normalized, connector_cfg)


def resolve_ingest_connector(
    company: str | None = None,
    environment: str | None = None,
    *,
    path: Path | None = None,
) -> str:
    explicit = os.getenv("HIVEFLOW_SOURCE", "").strip()
    if explicit:
        return normalize_connector(explicit)

    selected_company, selected_environment = resolve_selection(
        company,
        environment,
        path=path,
    )
    env_config = get_environment_config(selected_company, selected_environment, path=path)
    connectors = list(iter_configured_connectors(env_config))
    if len(connectors) == 1:
        return connectors[0][0]

    connector = get_config_value(
        "ingest.connector",
        company=company,
        environment=environment,
        path=path,
        default="qbo",
    )
    return str(connector).strip().lower() or "qbo"


def is_protected_environment(environment: str) -> bool:
    return environment in PROTECTED_ENVIRONMENTS


def resolve_cdk_deploy_filter(
    *,
    company: str | None = None,
    environment: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve CDK deploy filters from explicit deploy-time inputs only.

    Unlike resolve_selection(), this ignores config.yaml defaults so prod stacks
    are never synthesized unless HIVEFLOW_ENVIRONMENT=prod or `-c environment=prod`.
    """
    selected_company = (company or os.getenv("HIVEFLOW_COMPANY", "")).strip() or None
    selected_environment = (environment or os.getenv("HIVEFLOW_ENVIRONMENT", "")).strip() or None
    return selected_company, selected_environment


def _is_placeholder_account(account: str) -> bool:
    normalized = account.strip().upper()
    return not normalized or normalized.startswith(PLACEHOLDER_ACCOUNT_PREFIXES)


def _resolve_runtime_aws_account() -> str | None:
    """Resolve the active AWS account from the runtime credential chain."""
    try:
        import boto3

        account = str(boto3.client("sts").get_caller_identity()["Account"]).strip()
    except Exception:
        return None
    return account or None


def resolve_aws_deploy_env(
    env_config: dict[str, Any],
    environment: str,
) -> tuple[str | None, str | None]:
    aws_cfg = env_config.get("aws", {})
    if not isinstance(aws_cfg, dict):
        aws_cfg = {}

    configured_account = str(aws_cfg.get("account", "")).strip()
    configured_region = str(aws_cfg.get("region", "")).strip()
    deploy_account = os.getenv("CDK_DEFAULT_ACCOUNT", "").strip()
    deploy_region = (
        os.getenv("CDK_DEFAULT_REGION", "").strip()
        or os.getenv("AWS_REGION", "").strip()
        or configured_region
        or None
    )

    if is_protected_environment(environment):
        if _is_placeholder_account(configured_account):
            raise ValueError(
                f"Refusing to deploy {environment!r}: set companies.*.environments.{environment}.aws.account "
                "in config.yaml to the production AWS account ID before deploying."
            )
        if deploy_account and configured_account != deploy_account:
            raise ValueError(
                f"Refusing to deploy {environment!r} to AWS account {deploy_account}. "
                f"Config expects account {configured_account}."
            )
        return configured_account, deploy_region

    if configured_account:
        if _is_placeholder_account(configured_account):
            configured_account = ""
        elif deploy_account and configured_account != deploy_account:
            raise ValueError(
                f"Refusing to deploy {environment!r} to AWS account {deploy_account}. "
                f"Config expects account {configured_account}."
            )

    account = configured_account or deploy_account or _resolve_runtime_aws_account()
    return account, deploy_region


def iter_cdk_deploy_targets(
    *,
    company: str | None = None,
    environment: str | None = None,
    path: Path | None = None,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    config = load_project_config(path)
    deploy_company, deploy_environment = resolve_cdk_deploy_filter(
        company=company,
        environment=environment,
    )

    if deploy_company or deploy_environment:
        selected_company, selected_environment = resolve_selection(
            deploy_company,
            deploy_environment,
            path=path,
        )
        if is_protected_environment(selected_environment) and not deploy_environment:
            raise ValueError(
                f"Refusing to synthesize protected environment {selected_environment!r}. "
                "Set HIVEFLOW_ENVIRONMENT=prod or pass `-c environment=prod` to deploy it."
            )
        yield (
            selected_company,
            selected_environment,
            get_environment_config(selected_company, selected_environment, path=path),
        )
        return

    for company_name in _available_companies(config):
        for environment_name in _available_environments(config, company_name):
            if is_protected_environment(environment_name):
                continue
            yield (
                company_name,
                environment_name,
                get_environment_config(company_name, environment_name, path=path),
            )

