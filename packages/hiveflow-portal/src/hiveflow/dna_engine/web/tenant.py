"""Per-request tenant resolution for the DNA Engine.

Mirrors ``hiveflow.spreadsheet_lab.web.tenant`` (bucket + assumed-role
credentials) but, unlike Spreadsheet Engine, DNA Engine needs a full
``DnaSettings`` (pack id, DNA source, ...) — it IS the DNA business logic, not
a thin consumer of it — so this wraps the same helpers the shell's own
strict/multi-tenant path (``portal.routes._portal_settings``) and its async
KPI Generator worker (``portal.tenant.resolve_tenant_dna_settings``) already
use, rather than re-deriving bucket resolution from scratch.

Raises ``PortalTenantUnresolved`` (never silently falls back to a shared
bucket) when a session's client id has no configured tenant — same
fail-closed contract as the shell and as Spreadsheet Engine's own
``TenantUnresolved``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, NamedTuple

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.auth import PortalTenantUnresolved
from hiveflow.dna.web.portal.config import ClientPortalConfig


def hosting_environment() -> str:
    return os.getenv("HIVEFLOW_ENVIRONMENT", "dev").strip() or "dev"


class TenantScope(NamedTuple):
    settings: DnaSettings
    client: ClientPortalConfig


@contextmanager
def tenant_scope(client_id: str) -> Iterator[TenantScope]:
    """Bind the rest of this request to ``client_id``'s tenant: resolve its
    ``DnaSettings`` + ``ClientPortalConfig`` and assume that company's data
    role for the duration of the ``with`` block."""
    from hiveflow.dna.web.portal.config import load_client_portal_config, load_platform_env_config
    from hiveflow.dna.web.portal.tenant import resolve_tenant_dna_settings
    from hiveflow.tenant_credentials import tenant_credentials

    environment = hosting_environment()
    dna_settings = resolve_tenant_dna_settings(client_id, environment)  # raises PortalTenantUnresolved

    env_config = load_platform_env_config(environment)
    client = load_client_portal_config(client_id, env_config, default_pack_id=dna_settings.pack_id)

    with tenant_credentials(dna_settings.company, environment):
        yield TenantScope(settings=dna_settings, client=client)


__all__ = ["TenantScope", "hosting_environment", "tenant_scope", "PortalTenantUnresolved"]
