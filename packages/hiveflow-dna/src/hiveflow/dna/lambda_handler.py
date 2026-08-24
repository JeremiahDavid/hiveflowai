from __future__ import annotations

from typing import Any

from hiveflow.dna.compile import compile_pack
from hiveflow.dna.publish import publish_staging
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.validate import run_validation
from hiveflow.dna.workflow import load_production_pack


def run_dna_pipeline(
    settings: DnaSettings,
    *,
    silver_sql_pack_version: str = "",
) -> dict[str, Any]:
    from hiveflow.dna.init_client import ensure_client_governance
    from hiveflow.dna.publish import write_gold_refresh_manifest
    from hiveflow.dna.sql_runtime import apply_gold_sql_pack, has_gold_sql

    governance_init = ensure_client_governance(settings)

    # Athena gold SQL path: deterministic replay of approved SQL (no Bedrock).
    if has_gold_sql(settings):
        gold_sql = apply_gold_sql_pack(settings)
        pack_version = str(gold_sql.get("pack_version") or "").strip()
        manifest = write_gold_refresh_manifest(
            settings,
            pack_version=pack_version,
            silver_sql_pack_version=silver_sql_pack_version or pack_version,
            mode="athena_sql",
            extra={"gold_sql": gold_sql},
        )
        return {
            "status": "published",
            "mode": "athena_sql",
            "governance_init": governance_init,
            "gold_sql": gold_sql,
            "manifest": manifest,
        }

    pack = load_production_pack(settings)
    compile_manifest = compile_pack(settings, pack)
    if silver_sql_pack_version:
        compile_manifest["silver_sql_pack_version"] = silver_sql_pack_version
    validation_result = run_validation(settings, pack)
    if validation_result["status"] != "passed":
        return {
            "status": "validation_failed",
            "governance_init": governance_init,
            "compile": compile_manifest,
            "validation": validation_result,
        }
    publish_manifest = publish_staging(
        settings,
        compile_manifest=compile_manifest,
        validation_result=validation_result,
    )
    return {
        "status": "published",
        "mode": "python_compile",
        "governance_init": governance_init,
        "compile": compile_manifest,
        "validation": validation_result,
        "publish": publish_manifest,
    }


def _cfn_governance_init(event: dict[str, Any]) -> dict[str, Any]:
    """CloudFormation Provider onEvent — seed governance or no-op on Delete."""
    from hiveflow.dna.init_client import ensure_client_governance
    from hiveflow.dna.runtime import resolve_dna_settings

    request_type = str(event.get("RequestType", ""))
    props = event.get("ResourceProperties") or {}
    if not isinstance(props, dict):
        props = {}
    company = str(props.get("company") or "").strip()
    pack_id = str(props.get("pack_id") or "").strip()
    physical_id = str(
        event.get("PhysicalResourceId") or f"governance-init-{pack_id or company or 'default'}"
    )

    if request_type == "Delete":
        return {
            "PhysicalResourceId": physical_id,
            "Data": {"status": "delete_noop", "pack_id": pack_id},
        }

    settings = resolve_dna_settings(
        event={
            "action": "init-client",
            "company": company,
            "pack_id": pack_id,
        }
    )
    result = ensure_client_governance(settings)
    status = str(result.get("status", ""))
    if status not in {"initialized", "skipped"}:
        raise RuntimeError(f"Governance init failed: {result}")
    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "status": status,
            "pack_id": str(result.get("pack_id", pack_id)),
            "version": str(result.get("version", "")),
            "reason": str(result.get("reason", "")),
        },
    }


def handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    from hiveflow.dna.init_client import ensure_client_governance
    from hiveflow.dna.runtime import resolve_dna_settings

    payload = event or {}

    # CDK custom_resources.Provider / CloudFormation custom resource protocol
    if payload.get("RequestType") in {"Create", "Update", "Delete"}:
        return _cfn_governance_init(payload)

    settings = resolve_dna_settings(event=payload)
    action = str(payload.get("action", "publish")).strip().lower()

    if action in {"init-client", "init_client", "init-governance", "init_governance"}:
        return ensure_client_governance(settings)

    if action == "publish":
        return run_dna_pipeline(settings)
    if action in {"apply-gold-sql", "apply_gold_sql"}:
        from hiveflow.dna.sql_runtime import apply_gold_sql_pack

        return apply_gold_sql_pack(settings)
    if action in {"apply-silver-sql", "apply_silver_sql"}:
        from hiveflow.dna.sql_runtime import apply_silver_sql_pack

        return apply_silver_sql_pack(settings, source=str(payload.get("source") or settings.source))
    if action in {"refresh-data-profile", "refresh_data_profile"}:
        from hiveflow.dna.data_profile import refresh_data_profile_for_source

        return refresh_data_profile_for_source(
            settings,
            source=str(payload.get("source") or settings.source),
            entities=payload.get("entities"),
        )

    pack = load_production_pack(settings)
    if action == "compile":
        return compile_pack(settings, pack)
    if action == "validate":
        compile_pack(settings, pack)
        return run_validation(settings, pack)
    raise ValueError(f"Unknown DNA action {action!r}")


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    return handler(event, context)
