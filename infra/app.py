#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

INFRA_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INFRA_DIR.parent
sys.path.insert(0, str(INFRA_DIR))
for _pkg in (
    "hiveflow-platform",
    "hiveflow-connectors",
    "hiveflow-lake",
    "hiveflow-dna",
    "hiveflow-portal",
    "hiveflow",
):
    _src = PROJECT_ROOT / "packages" / _pkg / "src"
    if _src.is_dir():
        sys.path.insert(0, str(_src))

import aws_cdk as cdk
from cdk_scope import resolve_cdk_scope
from hiveflow.project_config import (
    dna_stack_module_name,
    dna_stack_name,
    get_dna_config,
    get_platform_config,
    get_ui_config,
    global_agent_pipelines_stack_module_name,
    global_agent_pipelines_stack_name,
    global_dna_engine_stack_module_name,
    global_dna_engine_stack_name,
    global_dns_stack_module_name,
    global_dns_stack_name,
    global_dna_stack_module_name,
    global_dna_stack_name,
    global_ui_stack_module_name,
    global_ui_stack_name,
    global_ui_web_api_export_name,
    ingest_stack_module_name,
    ingest_stack_name,
    is_dna_stack_enabled,
    is_platform_ui_enabled,
    is_global_dns_stack_enabled,
    iter_cdk_deploy_targets,
    iter_configured_connectors,
    iter_platform_deploy_environments,
    iter_portal_reporting_clients,
    platform_admin_stack_module_name,
    platform_admin_stack_name,
    platform_admin_web_api_export_name,
    portal_stack_module_name,
    portal_stack_name,
    portal_web_api_export_name,
    provisioning_stack_module_name,
    provisioning_stack_name,
    reporting_stack_module_name,
    reporting_stack_name,
    reporting_web_api_export_name,
    resolve_aws_deploy_env,
    resolve_data_bucket_name,
    resolve_dna_source,
    resolve_portal_client_buckets,
    resolve_qbo_secret_name,
)

app = cdk.App()


def _resolve_web_api_id(*, context_key: str, export_name: str) -> str:
    """Use a CDK context override during migration, otherwise import a stable stack export."""
    override = app.node.try_get_context(context_key)
    if override:
        return str(override).strip()
    return cdk.Fn.import_value(export_name)


def _reporting_web_api_context_key(client_id: str) -> str:
    slug = client_id.strip().lower().replace("_", "-")
    return f"{slug}ReportingWebApiId"


def _dns_manage_base_path_mappings() -> bool:
    value = app.node.try_get_context("dnsManageBasePathMappings")
    if value is None:
        return True
    return str(value).strip().lower() not in ("0", "false", "no")

filter_company = app.node.try_get_context("company") or os.getenv("HIVEFLOW_COMPANY")
filter_company_key = filter_company.strip().lower() if filter_company else None
filter_environment = app.node.try_get_context("environment") or os.getenv("HIVEFLOW_ENVIRONMENT")
cdk_scope = resolve_cdk_scope(
    context=app.node.try_get_context("scope"),
    env=os.getenv("HIVEFLOW_CDK_SCOPE"),
)

platform_config = get_platform_config()
platform_enabled = bool(platform_config.get("environments"))

if cdk_scope in ("all", "ingest"):
    for company, environment, env_config in iter_cdk_deploy_targets(
        company=filter_company,
        environment=filter_environment,
    ):
        connectors = list(iter_configured_connectors(env_config))
        if not connectors:
            continue

        account, region = resolve_aws_deploy_env(env_config, environment)
        raw_bucket_name = resolve_data_bucket_name(
            company,
            environment,
            account=account,
            region=region,
        )

        stack_id = ingest_stack_name(company, environment)
        module_name = ingest_stack_module_name(company)
        stack_module = importlib.import_module(f"stacks.{module_name}")

        secret_names = {
            connector: resolve_qbo_secret_name(company, environment, source=connector)
            for connector, _ in connectors
        }

        stack_module.IngestStack(
            app,
            stack_id,
            company=company,
            environment=environment,
            raw_bucket_name=raw_bucket_name,
            connectors=connectors,
            secret_names=secret_names,
            env=cdk.Environment(
                account=account,
                region=region,
            ),
            description=f"HiveFlow raw ingest for {company} ({environment})",
        )

        if is_dna_stack_enabled(env_config):
            dna_module_name = dna_stack_module_name(company)
            dna_module = importlib.import_module(f"stacks.{dna_module_name}")

            dna_module.DnaStack(
                app,
                dna_stack_name(company, environment),
                company=company,
                environment=environment,
                data_bucket_name=raw_bucket_name,
                source=resolve_dna_source(env_config),
                dna_config=get_dna_config(env_config),
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=f"HiveFlow DNA semantic engine for {company}/{environment}",
            )

if cdk_scope in ("all", "platform", "agent_pipelines") and platform_enabled:
    # GlobalAgentPipelinesStack imports its portal session secret by name
    # (see hiveflow.project_config.portal_session_secret_name) rather than
    # taking a live GlobalUiStack reference, so it's the only platform stack
    # this module needs under `-c scope=agent_pipelines` — everything else in
    # this block is skipped in that scope, which is what actually avoids
    # bundling every sibling platform Lambda (ui/reporting/full profiles) just
    # to deploy the Spreadsheet Engine. See infra/cdk_scope.py.
    global_agent_pipelines_module = importlib.import_module(
        f"stacks.{global_agent_pipelines_stack_module_name()}"
    )

    if cdk_scope in ("all", "platform"):
        global_ui_module = importlib.import_module(f"stacks.{global_ui_stack_module_name()}")
        global_dns_module = importlib.import_module(f"stacks.{global_dns_stack_module_name()}")
        global_dna_module = importlib.import_module(f"stacks.{global_dna_stack_module_name()}")
        portal_module = importlib.import_module(f"stacks.{portal_stack_module_name()}")
        platform_admin_module = importlib.import_module(f"stacks.{platform_admin_stack_module_name()}")
        provisioning_module = importlib.import_module(f"stacks.{provisioning_stack_module_name()}")

        # Legacy per-client ReportingStack + per-client reporting DNS records. Off
        # by default now that PortalStack serves every client through the
        # wildcard domain; `-c legacyReporting=true` keeps them for the
        # cut-over / rollback window (see the Phase 9 plan).
        legacy_reporting = str(
            app.node.try_get_context("legacyReporting") or ""
        ).strip().lower() in ("1", "true", "yes")
        reporting_module = (
            importlib.import_module(f"stacks.{reporting_stack_module_name()}")
            if legacy_reporting
            else None
        )

    for environment, platform_env_config in iter_platform_deploy_environments():
        if filter_environment and environment != filter_environment:
            continue

        account, region = resolve_aws_deploy_env(platform_env_config, environment)
        ui_config = get_ui_config(platform_env_config)

        global_agent_pipelines_module.GlobalAgentPipelinesStack(
            app,
            global_agent_pipelines_stack_name(environment),
            environment=environment,
            ui_config=ui_config,
            portal_ui_enabled=is_platform_ui_enabled(platform_env_config),
            env=cdk.Environment(
                account=account,
                region=region,
            ),
            description=f"Shared multi-tenant AI-agent pipelines (Spreadsheet Engine) for {environment}",
        )

        if cdk_scope not in ("all", "platform"):
            continue

        dns_stack_enabled = is_global_dns_stack_enabled(platform_env_config)
        client_buckets = resolve_portal_client_buckets(
            platform_env_config,
            environment,
            account=account,
            region=region,
        )

        global_dna_module.GlobalDnaStack(
            app,
            global_dna_stack_name(environment),
            environment=environment,
            env=cdk.Environment(
                account=account,
                region=region,
            ),
            description=f"Global DNA jobs (source documentation) for {environment}",
        )

        global_ui_stack = None
        if is_platform_ui_enabled(platform_env_config):
            global_ui_stack = global_ui_module.GlobalUiStack(
                app,
                global_ui_stack_name(environment),
                environment=environment,
                ui_config=ui_config,
                client_buckets=client_buckets,
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=f"Global HiveFlowAI UI for {environment}",
            )

        platform_admin_stack = None
        if is_platform_ui_enabled(platform_env_config):
            platform_admin_stack = platform_admin_module.PlatformAdminStack(
                app,
                platform_admin_stack_name(environment),
                environment=environment,
                ui_config=ui_config,
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=f"Platform admin UI for {environment}",
            )

            provisioning_module.ProvisioningStack(
                app,
                provisioning_stack_name(environment),
                environment=environment,
                config_bucket=platform_admin_stack.config_bucket,
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=f"Client onboarding CodeBuild provisioner for {environment}",
            )

        portal_stack = None
        if global_ui_stack is not None:
            portal_stack = portal_module.PortalStack(
                app,
                portal_stack_name(environment),
                environment=environment,
                ui_config=ui_config,
                portal_user_pool=global_ui_stack.portal_user_pool,
                portal_user_pool_client=global_ui_stack.portal_user_pool_client,
                portal_session_secret=global_ui_stack.portal_session_secret,
                domain_config=ui_config.get("domain", {}) if isinstance(ui_config.get("domain"), dict) else {},
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=f"Multi-tenant client reporting portal for {environment}",
            )

        reporting_stacks: list[tuple[str, dict, Any]] = []
        for client_id, reporting_company, client_cfg in (
            iter_portal_reporting_clients(platform_env_config) if legacy_reporting else []
        ):
            if filter_company_key and reporting_company.strip().lower() != filter_company_key:
                continue

            company_env_config = None
            try:
                from hiveflow.project_config import get_environment_config

                company_env_config = get_environment_config(reporting_company, environment)
            except KeyError:
                continue

            if not is_dna_stack_enabled(company_env_config):
                continue

            if global_ui_stack is None:
                continue

            reporting_bucket = resolve_data_bucket_name(
                reporting_company,
                environment,
                account=account,
                region=region,
            )
            reporting_stack = reporting_module.ReportingStack(
                app,
                reporting_stack_name(client_id, environment),
                client_id=client_id,
                company=reporting_company,
                environment=environment,
                data_bucket_name=reporting_bucket,
                source=resolve_dna_source(company_env_config),
                client_config=client_cfg,
                dna_config=get_dna_config(company_env_config),
                portal_user_pool=global_ui_stack.portal_user_pool,
                portal_user_pool_client=global_ui_stack.portal_user_pool_client,
                portal_session_secret=global_ui_stack.portal_session_secret,
                domain_config=ui_config.get("domain", {}) if isinstance(ui_config.get("domain"), dict) else {},
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=(
                    f"Portal reporting UI for client {client_id} "
                    f"({reporting_company}/{environment})"
                ),
            )
            reporting_stacks.append((client_id, client_cfg, reporting_stack))

        if dns_stack_enabled:
            from stacks.global_dns_stack import AdminDnsTarget, ReportingDnsTarget

            domain_cfg = ui_config.get("domain", {}) if isinstance(ui_config.get("domain"), dict) else {}
            admin_hostname = str(domain_cfg.get("admin_hostname", "admin")).strip().lower() or "admin"
            admin_dns_target = None
            if platform_admin_stack is not None:
                admin_dns_target = AdminDnsTarget(
                    rest_api_id=_resolve_web_api_id(
                        context_key="adminWebApiId",
                        export_name=platform_admin_web_api_export_name(environment),
                    ),
                    admin_hostname=admin_hostname,
                )

            # Per-client reporting subdomains are legacy — `*.{zone}` now routes
            # every client to PortalStack. Kept only during the cut-over window.
            dns_reporting_targets: list[ReportingDnsTarget] = []
            if legacy_reporting:
                for dns_client_id, reporting_company, client_cfg in iter_portal_reporting_clients(
                    platform_env_config
                ):
                    try:
                        from hiveflow.project_config import get_environment_config

                        company_env_config = get_environment_config(reporting_company, environment)
                    except KeyError:
                        continue
                    if not is_dna_stack_enabled(company_env_config):
                        continue
                    dns_reporting_targets.append(
                        ReportingDnsTarget(
                            rest_api_id=_resolve_web_api_id(
                                context_key=_reporting_web_api_context_key(dns_client_id),
                                export_name=reporting_web_api_export_name(dns_client_id, environment),
                            ),
                            client_id=dns_client_id,
                            reporting_hostname=str(
                                client_cfg.get("reporting_hostname", dns_client_id)
                            ).strip().lower(),
                        )
                    )

            wildcard_portal_api_id = None
            if portal_stack is not None:
                wildcard_portal_api_id = _resolve_web_api_id(
                    context_key="portalWebApiId",
                    export_name=portal_web_api_export_name(environment),
                )

            global_dns_module.GlobalDnsStack(
                app,
                global_dns_stack_name(environment),
                environment=environment,
                ui_config=ui_config,
                global_rest_api_id=_resolve_web_api_id(
                    context_key="globalWebApiId",
                    export_name=global_ui_web_api_export_name(environment),
                ),
                reporting_targets=dns_reporting_targets,
                wildcard_portal_api_id=wildcard_portal_api_id,
                admin_target=admin_dns_target,
                manage_base_path_mappings=_dns_manage_base_path_mappings(),
                env=cdk.Environment(
                    account=account,
                    region=region,
                ),
                description=f"HiveFlowAI public DNS for {environment}",
            )

if cdk_scope in ("all", "platform", "dna_engine") and platform_enabled:
    # GlobalDnaEngineStack imports its portal session secret by name and the
    # Cognito user pool/client by SSM parameter name (see
    # hiveflow.project_config.portal_session_secret_name /
    # portal_user_pool_id_parameter_name), rather than taking live GlobalUiStack
    # references — same reasoning GlobalAgentPipelinesStack's import-by-name
    # gets it, so `-c scope=dna_engine` alone deploys without constructing
    # GlobalUiStack (or GlobalAgentPipelinesStack) and paying for their bundling.
    global_dna_engine_module = importlib.import_module(
        f"stacks.{global_dna_engine_stack_module_name()}"
    )

    for environment, platform_env_config in iter_platform_deploy_environments():
        if filter_environment and environment != filter_environment:
            continue

        account, region = resolve_aws_deploy_env(platform_env_config, environment)
        ui_config = get_ui_config(platform_env_config)

        global_dna_engine_module.GlobalDnaEngineStack(
            app,
            global_dna_engine_stack_name(environment),
            environment=environment,
            ui_config=ui_config,
            portal_ui_enabled=is_platform_ui_enabled(platform_env_config),
            env=cdk.Environment(
                account=account,
                region=region,
            ),
            description=(
                "DNA Engine (catalog, governance, data profile, model mapping, "
                f"source docs, KPI Generator) for {environment}"
            ),
        )

app.synth()
