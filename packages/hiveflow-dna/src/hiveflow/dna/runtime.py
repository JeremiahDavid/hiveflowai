from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hiveflow.config import DEFAULT_DATA_DIR
from hiveflow.dna.settings import DnaSettings
from hiveflow.project_config import (
    get_dna_config,
    get_environment_config,
    get_ui_config,
    resolve_aws_deploy_env,
    resolve_data_bucket_name,
    resolve_dna_source,
    resolve_raw_bucket_name,
    resolve_selection,
)
from hiveflow.storage.paths import company_dna_config_id


def resolve_dna_settings(*, event: dict[str, Any] | None = None) -> DnaSettings:
    """Build DNA settings from env vars and optional Lambda/API event overrides.

    Gold compile always targets ``{company}_dna_config`` unless an explicit pack_id
    override is provided (tests / advanced tooling).
    """
    company, environment = resolve_selection()
    ui_mode = os.getenv("HIVEFLOW_UI_MODE", "").strip().lower()
    platform_ui = (
        os.getenv("HIVEFLOW_PLATFORM_UI", "").strip().lower() in ("1", "true", "yes")
        or ui_mode in ("global", "reporting_multitenant")
    )

    if platform_ui:
        from hiveflow.project_config import get_platform_environment_config

        try:
            env_config = get_platform_environment_config(environment)
        except KeyError:
            env_config = get_environment_config(company, environment)
    else:
        env_config = get_environment_config(company, environment)

    dna_cfg = get_dna_config(env_config)
    ui_cfg = get_ui_config(env_config)
    event_payload = event or {}

    event_company = str(event_payload.get("company", "")).strip()
    if event_company:
        company = event_company

    bucket = os.getenv("HIVEFLOW_S3_BUCKET", "").strip()
    if not bucket and event_company:
        # Async worker / CFN event names an explicit tenant — resolve its data
        # bucket even in platform-UI mode, where ``env_config`` is the
        # all-clients platform block rather than one company's config.
        try:
            bucket = resolve_data_bucket_name(event_company, environment)
        except (KeyError, ValueError):
            bucket = ""
    if not bucket and not platform_ui:
        account, region = resolve_aws_deploy_env(env_config, environment)
        bucket = resolve_raw_bucket_name(company, environment, account=account, region=region)

    source = str(event_payload.get("source", "")).strip().lower()
    if not source:
        source = os.getenv("HIVEFLOW_DNA_SOURCE", "").strip().lower()
    if not source:
        source = resolve_dna_source(env_config)

    explicit_pack = str(
        event_payload.get("pack_id")
        or os.getenv("HIVEFLOW_DNA_PACK_ID")
        or ""
    ).strip()
    # Prefer company DNA config for gold; allow explicit override only when set and
    # not the legacy starter id from older stack env vars.
    if explicit_pack and explicit_pack not in {"bc_intra_v1", "dbc_dna_boilerplate"}:
        pack_id = explicit_pack
    elif explicit_pack.endswith("_dna_config"):
        pack_id = explicit_pack
    else:
        pack_id = company_dna_config_id(company)
        # Ignore stale config.yaml pack_id pointing at starter templates.
        _ = ui_cfg.get("pack_id") or dna_cfg.get("pack_id")

    pack_version = event_payload.get("pack_version") or os.getenv("HIVEFLOW_DNA_PACK_VERSION")
    if pack_version is not None:
        pack_version = str(pack_version).strip() or None

    return DnaSettings(
        source=source or "dbc",
        data_dir=Path(os.getenv("HIVEFLOW_DATA_DIR", str(DEFAULT_DATA_DIR))),
        s3_bucket=bucket or None,
        company=company,
        pack_id=pack_id,
        pack_version=pack_version,
    )
