"""Per-client manual DNA refresh quota, status, and Step Functions trigger."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from hiveflow.compat import UTC
from typing import Any, Callable

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import read_json_artifact, write_json_artifact

_LOGGER = logging.getLogger(__name__)

DEFAULT_MONTHLY_LIMIT = 10
_EXECUTION_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class ManualRefreshQuotaExceeded(Exception):
    """Raised when a client has exhausted their monthly manual refresh allowance."""

    def __init__(self, *, monthly_limit: int, used: int) -> None:
        self.monthly_limit = monthly_limit
        self.used = used
        super().__init__(
            f"Manual DNA refresh limit reached ({used} of {monthly_limit} used this month). "
            "Try again next month or contact your administrator."
        )


class ManualRefreshInProgress(Exception):
    """Raised when a prior manual refresh is still running."""

    def __init__(self, *, execution_arn: str) -> None:
        self.execution_arn = execution_arn
        super().__init__(
            "A DNA refresh is already in progress. "
            "Wait for it to finish before starting another."
        )


@dataclass(frozen=True)
class ManualRefreshQuota:
    client_id: str
    month: str
    used: int
    monthly_limit: int
    remaining: int
    at_limit: bool
    in_progress: bool
    last_execution_arn: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "month": self.month,
            "used": self.used,
            "monthly_limit": self.monthly_limit,
            "remaining": self.remaining,
            "at_limit": self.at_limit,
            "in_progress": self.in_progress,
            "last_execution_arn": self.last_execution_arn,
        }


@dataclass(frozen=True)
class GoldRefreshStatus:
    pinned_version: str
    published_version: str
    published_at: str
    is_stale: bool
    has_gold_manifest: bool
    silver_applied_version: str = ""
    has_silver_transforms: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pinned_version": self.pinned_version,
            "published_version": self.published_version,
            "published_at": self.published_at,
            "is_stale": self.is_stale,
            "has_gold_manifest": self.has_gold_manifest,
            "silver_applied_version": self.silver_applied_version,
            "has_silver_transforms": self.has_silver_transforms,
        }


def resolve_monthly_limit(*, monthly_limit: int | None = None) -> int:
    if monthly_limit is not None and monthly_limit > 0:
        return int(monthly_limit)
    raw = os.getenv("HIVEFLOW_DNA_MANUAL_REFRESH_MONTHLY_LIMIT", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_MONTHLY_LIMIT


def current_usage_month(*, now: datetime | None = None) -> str:
    stamped = now or datetime.now(UTC)
    return stamped.strftime("%Y-%m")


def manual_refresh_usage_key(*, client_id: str, month: str) -> str:
    slug = client_id.strip().lower() or "default"
    return f"governance/_usage/manual_dna_refresh/{slug}/{month.strip()}.json"


def load_monthly_usage(
    settings: DnaSettings,
    *,
    client_id: str,
    month: str | None = None,
) -> dict[str, Any]:
    usage_month = month or current_usage_month()
    payload = read_json_artifact(
        settings,
        manual_refresh_usage_key(client_id=client_id, month=usage_month),
    ) or {}
    refreshes = payload.get("refreshes")
    if not isinstance(refreshes, list):
        refreshes = []
    return {
        "client_id": str(payload.get("client_id") or client_id or "").strip(),
        "month": usage_month,
        "count": int(payload.get("count") or len(refreshes)),
        "refreshes": refreshes,
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _describe_execution_status(
    execution_arn: str,
    *,
    describe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    if not execution_arn.strip():
        return ""
    try:
        if describe_fn is not None:
            payload = describe_fn(execution_arn)
        elif os.getenv("HIVEFLOW_DNA_REFRESH_MOCK", "").strip().lower() in {"1", "true", "yes"}:
            return "SUCCEEDED"
        else:
            from hiveflow.storage.aws import sfn_client

            client = sfn_client()
            payload = client.describe_execution(executionArn=execution_arn)
        return str(payload.get("status") or "").strip().upper()
    except Exception as exc:  # noqa: BLE001 — stale ARNs / IAM gaps must not 500 the portal
        _LOGGER.warning(
            "Could not describe DNA refresh execution %s: %s",
            execution_arn,
            exc,
        )
        return ""


def _last_refresh_in_progress(
    usage: dict[str, Any],
    *,
    describe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    refreshes = usage.get("refreshes")
    if not isinstance(refreshes, list) or not refreshes:
        return False, ""
    last = refreshes[-1]
    if not isinstance(last, dict):
        return False, ""
    execution_arn = str(last.get("execution_arn") or "").strip()
    if not execution_arn:
        return False, ""
    status = _describe_execution_status(execution_arn, describe_fn=describe_fn)
    if status in {"RUNNING", "PENDING_REDRIVE"}:
        return True, execution_arn
    return False, execution_arn


def quota_summary(
    settings: DnaSettings,
    *,
    client_id: str,
    monthly_limit: int | None = None,
    month: str | None = None,
    describe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> ManualRefreshQuota:
    limit = resolve_monthly_limit(monthly_limit=monthly_limit)
    usage = load_monthly_usage(settings, client_id=client_id, month=month)
    used = int(usage["count"])
    in_progress, last_execution_arn = _last_refresh_in_progress(
        usage,
        describe_fn=describe_fn,
    )
    remaining = max(0, limit - used)
    at_limit = used >= limit
    return ManualRefreshQuota(
        client_id=client_id,
        month=str(usage["month"]),
        used=used,
        monthly_limit=limit,
        remaining=remaining,
        at_limit=at_limit,
        in_progress=in_progress,
        last_execution_arn=last_execution_arn,
    )


def gold_refresh_status(
    settings: DnaSettings,
    *,
    pinned_version: str,
) -> GoldRefreshStatus:
    from hiveflow.dna.sql_pack import load_sql_pack

    manifest = read_json_artifact(settings, f"{settings.gold_dna_prefix}/manifest.json") or {}
    published_version = str(manifest.get("pack_version") or "").strip()
    published_at = str(manifest.get("published_at") or "").strip()
    has_gold_manifest = bool(published_version or published_at or manifest.get("outputs"))
    silver_applied_version = str(manifest.get("silver_sql_pack_version") or "").strip()
    pinned = str(pinned_version or "").strip()
    pack = load_sql_pack(settings, version=pinned) if pinned else None
    has_silver_transforms = bool(pack and pack.by_layer("silver"))
    if has_silver_transforms and not silver_applied_version:
        from hiveflow.dna.source_docs.reference import load_silver_schema_profile

        profile = load_silver_schema_profile(settings, source=settings.source) or {}
        silver_applied_version = str(profile.get("silver_sql_pack_version") or "").strip()

    gold_stale = bool(pinned) and (not published_version or published_version != pinned)
    silver_stale = bool(
        has_silver_transforms and pinned and silver_applied_version != pinned
    )
    is_stale = gold_stale or silver_stale
    return GoldRefreshStatus(
        pinned_version=pinned,
        published_version=published_version,
        published_at=published_at,
        is_stale=is_stale,
        has_gold_manifest=has_gold_manifest,
        silver_applied_version=silver_applied_version,
        has_silver_transforms=has_silver_transforms,
    )


def _resolve_state_machine_arn(*, company: str, environment: str) -> str:
    explicit = os.getenv("HIVEFLOW_DNA_REFRESH_STATE_MACHINE_ARN", "").strip()
    if explicit:
        return explicit
    from hiveflow.process_config import Process, step_function_name_for_process

    name = step_function_name_for_process(company, environment, "all", Process.DNA_REFRESH)
    region = (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("HIVEFLOW_AWS_REGION")
        or "us-east-2"
    )
    import boto3

    account = boto3.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:states:{region}:{account}:stateMachine:{name}"


def _sanitize_execution_name(*, client_id: str) -> str:
    slug = _EXECUTION_NAME_RE.sub("-", client_id.strip().lower() or "client")
    suffix = uuid.uuid4().hex[:10]
    name = f"portal-{slug}-{suffix}"
    return name[:80]


def _start_refresh_execution(
    *,
    company: str,
    environment: str,
    client_id: str,
    username: str,
    pinned_version: str,
    start_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "trigger": "portal_manual",
        "client_id": client_id,
        "username": username,
        "pinned_version": pinned_version,
    }
    if start_fn is not None:
        state_machine_arn = _resolve_state_machine_arn(company=company, environment=environment)
        execution_name = _sanitize_execution_name(client_id=client_id)
        return start_fn(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(payload),
        )

    if os.getenv("HIVEFLOW_DNA_REFRESH_MOCK", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "executionArn": (
                f"arn:aws:states:us-east-2:000000000000:execution:mock-dna-refresh:{uuid.uuid4().hex}"
            ),
            "startDate": datetime.now(UTC),
        }

    state_machine_arn = _resolve_state_machine_arn(company=company, environment=environment)
    execution_name = _sanitize_execution_name(client_id=client_id)
    from hiveflow.storage.aws import sfn_client

    client = sfn_client()
    return client.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(payload),
    )


def record_manual_refresh(
    settings: DnaSettings,
    *,
    client_id: str,
    username: str,
    pinned_version: str,
    execution_arn: str,
    month: str | None = None,
) -> dict[str, Any]:
    usage_month = month or current_usage_month()
    current = load_monthly_usage(settings, client_id=client_id, month=usage_month)
    refreshes = list(current.get("refreshes") or [])
    refreshes.append(
        {
            "at": datetime.now(UTC).isoformat(),
            "username": username,
            "pinned_version": pinned_version,
            "execution_arn": execution_arn,
        }
    )
    payload = {
        "client_id": client_id,
        "month": usage_month,
        "count": len(refreshes),
        "refreshes": refreshes,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    write_json_artifact(
        settings,
        manual_refresh_usage_key(client_id=client_id, month=usage_month),
        payload,
    )
    return payload


def trigger_manual_refresh(
    settings: DnaSettings,
    *,
    client_id: str,
    username: str,
    pinned_version: str,
    company: str,
    environment: str,
    monthly_limit: int | None = None,
    month: str | None = None,
    start_fn: Callable[..., dict[str, Any]] | None = None,
    describe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    quota = quota_summary(
        settings,
        client_id=client_id,
        monthly_limit=monthly_limit,
        month=month,
        describe_fn=describe_fn,
    )
    if quota.in_progress:
        raise ManualRefreshInProgress(execution_arn=quota.last_execution_arn)
    if quota.at_limit:
        raise ManualRefreshQuotaExceeded(
            monthly_limit=quota.monthly_limit,
            used=quota.used,
        )

    response = _start_refresh_execution(
        company=company,
        environment=environment,
        client_id=client_id,
        username=username,
        pinned_version=pinned_version,
        start_fn=start_fn,
    )
    execution_arn = str(response.get("executionArn") or "").strip()
    if not execution_arn:
        raise RuntimeError("DNA refresh did not return an execution ARN")

    usage = record_manual_refresh(
        settings,
        client_id=client_id,
        username=username,
        pinned_version=pinned_version,
        execution_arn=execution_arn,
        month=month,
    )
    return {
        "execution_arn": execution_arn,
        "quota": quota_summary(
            settings,
            client_id=client_id,
            monthly_limit=monthly_limit,
            month=month,
            describe_fn=describe_fn,
        ).to_dict(),
        "usage": usage,
    }
