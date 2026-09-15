"""Backward-compatible re-export.

Relocated to ``hiveflow.tenant_credentials`` (hiveflow-platform) so it can be
shared by any global multi-tenant Lambda, not just the portal — e.g. the
Spreadsheet Engine's ``GlobalAgentPipelinesStack``. Kept here as a thin shim so
existing portal imports don't need to change.
"""

from __future__ import annotations

from hiveflow.tenant_credentials import (
    TenantCredentialsError,
    _cache,  # noqa: F401 — re-exported for tests that reset the module-level cache
    tenant_boto_session,
    tenant_credentials,
)

__all__ = ["TenantCredentialsError", "tenant_boto_session", "tenant_credentials"]
