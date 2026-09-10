"""Tenant resolution for contexts without an HTTP request.

The per-request path binds a tenant in ``routes._portal_settings``. Async Lambda
workers (KPI Generator self-invoke) and CloudFormation custom resources only carry
a ``client_id`` in their event, so they rebuild the same ``DnaSettings`` here.
"""

from __future__ import annotations

import os
from pathlib import Path

from hiveflow.config import DEFAULT_DATA_DIR
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.auth import PortalTenantUnresolved
from hiveflow.dna.web.portal.config import load_client_portal_config, load_platform_env_config


def resolve_tenant_dna_settings(client_id: str, environment: str) -> DnaSettings:
    """Build ``DnaSettings`` for ``client_id`` from the platform registry.

    Raises ``PortalTenantUnresolved`` when the client has no ``reporting_company``
    or its data bucket cannot be resolved — never returns env-var defaults.
    """
    from hiveflow.project_config import (
        get_environment_config,
        resolve_data_bucket_name,
        resolve_dna_source,
    )
    from hiveflow.storage.paths import company_dna_config_id

    normalized = str(client_id or "").strip().lower()
    if not normalized:
        raise PortalTenantUnresolved("No client_id supplied for tenant resolution.")

    env_config = load_platform_env_config(environment)
    client_cfg = load_client_portal_config(normalized, env_config, default_pack_id="")
    reporting_company = str(client_cfg.reporting_company or "").strip()
    if not reporting_company:
        raise PortalTenantUnresolved(
            f"Reporting is not configured for client {normalized}."
        )

    try:
        bucket = resolve_data_bucket_name(reporting_company, environment)
    except (KeyError, ValueError) as exc:
        raise PortalTenantUnresolved(
            f"Data store is not provisioned for client {normalized}."
        ) from exc

    company_env = get_environment_config(reporting_company, environment)
    return DnaSettings(
        source=resolve_dna_source(company_env),
        data_dir=Path(os.getenv("HIVEFLOW_DATA_DIR", str(DEFAULT_DATA_DIR))),
        s3_bucket=bucket or None,
        company=reporting_company,
        pack_id=company_dna_config_id(reporting_company),
    )
