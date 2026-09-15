"""Per-invocation tenant credential isolation.

A shared, multi-tenant Lambda's own role can touch nothing tenant-scoped. Before
any data access it assumes ``hiveflow-portal-tenant-{company}-{environment}`` (a
narrow role minted by that company's ``DnaStack``) and installs the resulting
boto3 session via ``hiveflow.storage.aws.use_boto_session`` for the rest of the
invocation. Credentials are cached per company until shortly before expiry.

AssumeRole is only attempted on Lambda (or when ``HIVEFLOW_TENANT_ASSUME_ROLE``
is set); everywhere else — single-tenant Lambdas, local dev, tests — this is a
no-op and the default credential chain is used.

Originally built for the multi-tenant portal Lambda (``PortalStack``); any other
shared/global Lambda that needs per-company data access (e.g. the Spreadsheet
Engine's ``GlobalAgentPipelinesStack``) should use this same helper rather than
growing its own copy.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from typing import Any, Iterator

from hiveflow.storage.aws import use_boto_session

logger = logging.getLogger("hiveflow.tenant")

# Refresh this many seconds before the assumed credentials actually expire.
_EXPIRY_SKEW_S = 300
_ASSUME_DURATION_S = 3600

_cache: dict[str, tuple[Any, float]] = {}
_cache_lock = threading.Lock()


class TenantCredentialsError(Exception):
    """The tenant role could not be assumed — the request must fail closed (503)."""


def _assume_role_enabled() -> bool:
    if os.getenv("HIVEFLOW_TENANT_ASSUME_ROLE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    if os.getenv("HIVEFLOW_TENANT_ASSUME_ROLE", "").strip().lower() in {"0", "false", "no"}:
        return False
    try:
        from hiveflow.project_config import is_lambda_runtime

        return bool(is_lambda_runtime())
    except Exception:  # noqa: BLE001
        return False


def _tenant_role_arn(company: str, environment: str, account: str) -> str:
    return (
        f"arn:aws:iam::{account}:role/hiveflow-portal-tenant-"
        f"{company}-{environment}"
    )


def _new_tenant_session(company: str, environment: str) -> tuple[Any, float]:
    import boto3

    sts = boto3.client("sts")
    account = os.getenv("HIVEFLOW_AWS_ACCOUNT_ID", "").strip()
    if not account:
        account = str(sts.get_caller_identity()["Account"]).strip()

    resp = sts.assume_role(
        RoleArn=_tenant_role_arn(company, environment, account),
        RoleSessionName=f"tenant-{company}"[:64],
        DurationSeconds=_ASSUME_DURATION_S,
    )
    creds = resp["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    return session, float(creds["Expiration"].timestamp())


def tenant_boto_session(company: str, environment: str) -> Any:
    """Assume the tenant role for ``company`` and return a cached boto3 Session."""
    key = company.strip().lower()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[1] - _EXPIRY_SKEW_S > now:
            return hit[0]
    try:
        session, expiry = _new_tenant_session(key, environment)
    except Exception as exc:  # noqa: BLE001 — surfaced as 503, never a silent fallback
        logger.error("tenant_assume_role_failed company=%s: %s", key, exc)
        raise TenantCredentialsError(
            f"Could not assume the data role for client company {key!r}."
        ) from exc
    with _cache_lock:
        _cache[key] = (session, expiry)
    logger.info("tenant_assume_role_ok company=%s expiry=%.0f", key, expiry)
    return session


@contextlib.contextmanager
def tenant_credentials(company: str, environment: str) -> Iterator[None]:
    """Run the block with ``company``'s assumed credentials installed.

    No-op (default credential chain) off Lambda or without an explicit opt-in.
    """
    if not company or not _assume_role_enabled():
        yield
        return
    session = tenant_boto_session(company, environment)
    with use_boto_session(session):
        yield
