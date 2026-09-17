"""Per-request tenant resolution for the Spreadsheet Engine.

Mirrors ``hiveflow.dna.web.portal.routes._portal_settings`` (strict/
multi-tenant branch) at a much smaller scope: this app only ever needs a
bucket name and assumed-role credentials, not a full ``DnaSettings`` (pack
id, DNA source, etc). Given the session's ``client_id``:

1. ``config.yaml``'s ``platform.environments.<env>.ui.portal.clients.<id>``
   resolves to a ``reporting_company``.
2. That company's own deploy config resolves an AWS account/region, which
   resolves its data bucket name.
3. ``hiveflow.tenant_credentials.tenant_credentials`` assumes
   ``hiveflow-portal-tenant-{company}-{environment}`` for the duration of the
   request — the same role every other multi-tenant portal request uses.

Raises ``TenantUnresolved`` (never silently falls back to a shared bucket)
when a session's client id has no configured tenant, matching
``PortalTenantUnresolved``'s fail-closed behavior in the main portal.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator


class TenantUnresolved(Exception):
    """A request's client id has no configured reporting tenant."""


def hosting_company() -> str:
    return os.getenv("HIVEFLOW_COMPANY", "poc").strip() or "poc"


def hosting_environment() -> str:
    return os.getenv("HIVEFLOW_ENVIRONMENT", "dev").strip() or "dev"


def _resolve_tenant_bucket(client_id: str, *, environment: str) -> tuple[str, str]:
    """Return ``(reporting_company, bucket)`` for ``client_id``, or raise."""
    from hiveflow.dna.web.portal.config import load_client_portal_config, load_platform_env_config
    from hiveflow.project_config import get_environment_config, resolve_aws_deploy_env, resolve_data_bucket_name

    env_config = load_platform_env_config(environment)
    client_config = load_client_portal_config(client_id, env_config, default_pack_id="")
    reporting_company = client_config.reporting_company
    if not reporting_company:
        raise TenantUnresolved(f"Reporting is not configured for client {client_id or 'this account'!r}.")

    try:
        client_env = get_environment_config(reporting_company, environment)
    except KeyError as exc:
        raise TenantUnresolved(
            f"Reporting company {reporting_company!r} is not configured for {environment}."
        ) from exc

    account, region = resolve_aws_deploy_env(client_env, environment)
    try:
        bucket = resolve_data_bucket_name(reporting_company, environment, account=account, region=region)
    except ValueError as exc:
        raise TenantUnresolved(f"Data store is not provisioned for client {client_id!r}.") from exc
    return reporting_company, bucket


@contextmanager
def tenant_scope(client_id: str) -> Iterator[str]:
    """Bind the rest of this request to ``client_id``'s tenant: sets
    ``HIVEFLOW_S3_BUCKET`` and assumes that company's data role for the
    duration of the ``with`` block. Yields the resolved company name."""
    from hiveflow.tenant_credentials import tenant_credentials

    environment = hosting_environment()
    reporting_company, bucket = _resolve_tenant_bucket(client_id, environment=environment)

    previous_bucket = os.environ.get("HIVEFLOW_S3_BUCKET")
    os.environ["HIVEFLOW_S3_BUCKET"] = bucket
    try:
        with tenant_credentials(reporting_company, environment):
            yield reporting_company
    finally:
        if previous_bucket is None:
            os.environ.pop("HIVEFLOW_S3_BUCKET", None)
        else:
            os.environ["HIVEFLOW_S3_BUCKET"] = previous_bucket
