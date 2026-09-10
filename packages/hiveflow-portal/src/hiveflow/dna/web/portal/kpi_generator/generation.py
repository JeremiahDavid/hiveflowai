"""KPI Generator — Bedrock proposal generation, working-session state, Lambda worker."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from hiveflow.compat import UTC
from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.source_docs.reference import load_source_docs_gold_artifact
from hiveflow.dna.store import list_json_artifact_keys, read_json_artifact, write_json_artifact
from hiveflow.dna.web.portal.governance_helpers.bedrock_usage import (
    BedrockBudgetExceeded,
    record_usage,
    usage_summary,
)
from hiveflow.dna.web.portal.kpi_generator.catalog import (
    _columns_with_companion_aliases,
    _prepare_implement_draft,
    _validate_layer_rules,
    build_allowed_joins,
    build_columns_by_table,
    format_allowed_joins_for_prompt,
    format_silver_columns_for_prompt,
    validation_criteria_from_proposal,
)
from hiveflow.dna.web.portal.kpi_generator.drafts import (
    GENERATION_COMPLETE,
    GENERATION_ERROR,
    GENERATION_PENDING,
    assistant_text_from_normalized,
    normalize_generated_payload,
    primary_draft,
)
from hiveflow.dna.web.portal.kpi_generator.paths import (
    kpi_generator_proposal_key,
    kpi_generator_proposals_prefix,
)
from hiveflow.dna.workflow import load_production_pack

DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MAX_KPI_CHAT_TURNS = 5
KPI_GENERATE_TASK = "kpi_generator_generate"

_LOGGER = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _source_docs_context(settings: DnaSettings) -> dict[str, Any]:
    return {
        "entity_relationships": load_source_docs_gold_artifact(settings, "entity_relationships") or {},
        "entity_property_tags": load_source_docs_gold_artifact(settings, "entity_property_tags") or {},
    }


def _source_docs_prompt_excerpt(context: dict[str, Any]) -> str:
    """Tags only — column names come from the silver_stg catalog, not MS Learn docs."""
    tags = context.get("entity_property_tags") or {}
    tables = []
    for table in tags.get("tables") or []:
        if not isinstance(table, dict):
            continue
        props = []
        for prop in table.get("properties") or []:
            if not isinstance(prop, dict) or prop.get("in_silver") is False:
                continue
            tag_list = [str(tag) for tag in (prop.get("tags") or []) if str(tag).strip()]
            if not tag_list:
                continue
            column = str(prop.get("silver_column") or prop.get("name") or "").strip()
            if column:
                props.append({"column": column, "tags": tag_list})
        if props:
            tables.append(
                {
                    "silver_entity": table.get("silver_entity"),
                    "properties": props,
                }
            )
    payload = {"in_silver_property_tags": tables}
    return json.dumps(payload, indent=2)[:4000]


def _sql_pack_context_for_prompt(settings: DnaSettings) -> str:
    """Pinned silver aliases and gold grains so the model can reuse existing DNA."""
    from hiveflow.dna.silver_enhancement import extract_new_column_aliases
    from hiveflow.dna.sql_pack import load_sql_pack, load_transform_sql

    try:
        pack = load_sql_pack(settings)
    except Exception:  # noqa: BLE001
        pack = None
    if pack is None:
        return "No pinned SQL pack yet (no approved silver columns or gold outputs)."
    lines = [f"Pinned production SQL pack v{pack.version}:"]
    for transform in pack.transforms:
        try:
            body = load_transform_sql(
                settings,
                transform,
                version=pack.version,
                verify_checksum=True,
            )
        except Exception:  # noqa: BLE001
            body = ""
        if transform.layer == "silver":
            aliases = extract_new_column_aliases(body) if body else []
            entity = str(transform.target_entity or "").strip() or transform.id
            alias_text = ", ".join(aliases) if aliases else "(no AS aliases parsed)"
            lines.append(f"- silver entity {entity} ({transform.id}): added columns {alias_text}")
        else:
            output_id = str(transform.output_id or transform.id).strip()
            grain = list(transform.grain_columns or [])
            grain_label = ", ".join(grain) if grain else "company total"
            desc = str(transform.description or "").strip()
            extra = f" — {desc}" if desc else ""
            lines.append(f"- gold output {output_id} grain=[{grain_label}]{extra}")
    text = "\n".join(lines)
    return text[:4000]


def _trim_kpi_chat_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep at most MAX_KPI_CHAT_TURNS user messages and their assistant replies."""
    if not history:
        return []
    user_indices = [
        index for index, entry in enumerate(history) if str(entry.get("role") or "") == "user"
    ]
    if len(user_indices) <= MAX_KPI_CHAT_TURNS:
        return history
    return history[user_indices[-MAX_KPI_CHAT_TURNS] :]


def _build_kpi_chat_messages(
    chat_history: list[dict[str, str]],
    *,
    prompt: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in chat_history:
        role = str(entry.get("role") or "").strip().lower()
        text = str(entry.get("text") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": prompt}]})
    return messages


def generate_kpi_proposal(
    settings: DnaSettings,
    *,
    prompt: str,
    client_id: str = "",
    monthly_budget_usd: float | None = None,
    username: str = "",
    prior_chat_history: list[dict[str, str]] | None = None,
    prior_validation_criteria: dict[str, Any] | None = None,
    prior_proposal_id: str = "",
) -> dict[str, Any]:
    """Call Bedrock once; store ephemeral proposal (not production SQL)."""
    text = (prompt or "").strip()
    if not text:
        raise ValueError("Prompt is required")

    summary = usage_summary(
        settings,
        client_id=client_id,
        monthly_budget_usd=monthly_budget_usd,
    )
    if summary.at_limit:
        raise BedrockBudgetExceeded(
            monthly_budget_usd=summary.monthly_budget_usd,
            estimated_cost_usd=summary.estimated_cost_usd,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        )

    context = _source_docs_context(settings)
    relationships = context.get("entity_relationships") or {}
    allowed_joins = build_allowed_joins(relationships, source=settings.source)
    allowed_joins_text = format_allowed_joins_for_prompt(allowed_joins)
    columns_by_table = build_columns_by_table(settings, entity_properties={})
    silver_columns_text = format_silver_columns_for_prompt(
        columns_by_table,
        prompt=text,
        allowed_joins=allowed_joins,
    )
    pack = None
    try:
        pack = load_production_pack(settings)
        pack_summary = {
            "entities": [e.silver_entity for e in pack.entities],
            "outputs": [o.id for o in pack.outputs],
            "kpis": [k.id for k in pack.kpis],
        }
    except Exception:  # noqa: BLE001
        pack_summary = {}
    sql_pack_text = _sql_pack_context_for_prompt(settings)
    source_docs_excerpt = _source_docs_prompt_excerpt(context)

    system = (
        "You are the HiveFlow KPI Generator, a DNA modeling assistant. "
        "Using the live silver_stg Glue catalog, the pinned SQL pack, allowed joins, "
        "and the DNA pack summary, decide how to respond to the user. "
        "Return ONLY JSON with intent plus the fields for that intent.\n"
        "intents:\n"
        "- clarify: the request is under-specified (missing business rule, field mapping, "
        "or membership list). Include questions (string array) and summary. No drafts.\n"
        "- reuse: existing DNA already answers it (pinned silver column and/or gold output "
        "with the same grain). Include reuse: {reason, output_id?, column?, sql?} where sql "
        "is optional session-only SELECT against dna_* / silver_* for preview. "
        "Do not create a new governance transform. Duplicate gold grains are rejected.\n"
        "- implement: new SQL is required. Include drafts (array, 1–2 items) plus summary. "
        "At most one silver and one gold draft. "
        "Silver-only when adding a reusable entity attribute not already in DNA silver. "
        "Gold-only when aggregating over existing DNA silver columns. "
        "Both when the request needs a new reusable attribute AND a new gold table; "
        "gold SQL MUST use the new silver column and must not re-inline the membership list.\n"
        "Heuristic: 'total interco sales by month' with no definition of interco → clarify. "
        "User supplies a customer list → implement silver customers.is_interco on "
        "silver_stg_* plus gold SUM(sales) by month on silver_* joined to that flag. "
        "Flag already in pinned silver SQL / catalog → implement gold-only, or reuse if a "
        "gold output already has that grain.\n"
        "Each implement draft object keys: layer, mode, id, target_entity, output_id, "
        "grain_columns, file, sql, fields_used (list), filters_applied (list of strings), "
        "calculation (string), summary (string). "
        "Silver: mode add_columns with target_entity; preserve entity primary-key grain "
        "(no GROUP BY, no SELECT DISTINCT, no top-level aggregates). "
        "Gold: mode fact_table or kpi with output_id; grain_columns required "
        "(empty list = company total). "
        "file is relative to sql/ with the layer prefix, e.g. "
        "silver/add_col__customers.sql or gold/kpi_net_revenue.sql. "
        "Athena SQL uses Glue table names only (no database prefix): "
        "silver column additions FROM silver_stg_{source}_{entity}; "
        "gold facts/KPIs FROM silver_{source}_{entity} (DNA-enhanced silver) "
        "and dna_{output_id}. Do not use silver. or gold. qualifiers. "
        "Column names (strict): use ONLY columns listed under silver_stg catalog below, plus "
        "aliases you add in a companion silver draft in the same response. "
        "Never invent columns and never use MS Learn / source-docs names that are absent "
        "from the catalog (for example paymentTermsCode on invoices — use paymentTermsId "
        "and JOIN payment_terms). "
        "When a dimension lives on another silver_stg table, JOIN it using allowed_joins. "
        "When adding a column from another table via JOIN, prefer correlated subqueries "
        "instead of GROUP BY when each base row maps to one joined row. "
        "Silver contributions must never use GROUP BY. "
        "JOIN rules (strict): use ONLY joins listed in allowed_joins below. "
        "Each JOIN ON must use the exact FK/PK columns shown. "
        "Do not invent join paths, bridge tables, or join keys. "
        "Single-table SELECTs are fine when no join is needed."
        f"\n\nSilver_stg catalog (authoritative for SQL):\n{silver_columns_text}\n\n"
        f"Allowed joins:\n{allowed_joins_text}\n\n"
        f"{sql_pack_text}\n\n"
        f"DNA pack summary:\n{json.dumps(pack_summary, indent=2)[:6000]}\n\n"
        f"In-silver property tags (truncated):\n{source_docs_excerpt}"
    )
    chat_history = _trim_kpi_chat_history(list(prior_chat_history or []))
    if not chat_history:
        user_prompt = f"User request:\n{text}"
    else:
        user_prompt = text
    bedrock_messages = _build_kpi_chat_messages(chat_history, prompt=user_prompt)

    import boto3

    model_id = os.environ.get("HIVEFLOW_BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
    client = boto3.client("bedrock-runtime")
    response = client.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=bedrock_messages,
        inferenceConfig={"maxTokens": 4096, "temperature": 0.2},
    )
    raw_text = _extract_converse_text(response)
    payload = _parse_json_object(raw_text)
    normalized = normalize_generated_payload(payload)
    intent = str(normalized.get("intent") or "implement")
    drafts = list(normalized.get("drafts") or [])
    if intent == "implement":
        existing_gold: list[dict[str, Any]] | None = None
        from hiveflow.dna.sql_pack import load_sql_pack

        sql_pack = load_sql_pack(settings)
        if sql_pack is not None:
            existing_gold = [
                transform.to_dict()
                for transform in sql_pack.transforms
                if transform.layer == "gold"
            ]
        columns_for_gold = _columns_with_companion_aliases(
            settings, drafts, columns_by_table=columns_by_table
        )
        for draft in drafts:
            _prepare_implement_draft(draft)
            extra_columns = (
                columns_for_gold
                if str(draft.get("layer") or "").strip().lower() == "gold"
                else columns_by_table
            )
            _validate_layer_rules(
                draft,
                settings=settings,
                relationships=relationships,
                pack=pack,
                existing_gold_transforms=existing_gold,
                columns_by_table=extra_columns,
            )
    primary = primary_draft(drafts)

    # Record token usage for the shared Bedrock budget meter.
    usage = response.get("usage") or {}
    in_tok = int(usage.get("inputTokens") or 0)
    out_tok = int(usage.get("outputTokens") or 0)
    if in_tok or out_tok:
        record_usage(
            settings,
            input_tokens=in_tok,
            output_tokens=out_tok,
            client_id=client_id,
        )

    chat_history = _trim_kpi_chat_history(
        chat_history
        + [
            {"role": "user", "text": text},
            {"role": "assistant", "text": assistant_text_from_normalized(normalized)},
        ]
    )
    now = datetime.now(UTC).isoformat()
    proposal_id = ""
    created_at = now
    prior_id = str(prior_proposal_id or "").strip()
    if prior_id:
        prior = load_kpi_proposal(settings, prior_id)
        if prior and str(prior.get("status") or "").strip().lower() == "working":
            proposal_id = prior_id
            created_at = str(prior.get("created_at") or now)
    if not proposal_id:
        proposal_id = uuid.uuid4().hex[:12]
    close_working_kpi_proposals(
        settings,
        username=username,
        keep_id=proposal_id,
    )
    proposal = {
        "proposal_id": proposal_id,
        "created_at": created_at,
        "username": username,
        "prompt": text,
        "chat_history": chat_history,
        "intent": intent,
        "questions": list(normalized.get("questions") or []),
        "reuse": normalized.get("reuse"),
        "drafts": drafts,
        "draft": primary,
        "status": "working",
        "generation_status": GENERATION_COMPLETE,
    }
    if prior_validation_criteria:
        proposal["last_validation"] = dict(prior_validation_criteria)
    write_json_artifact(
        settings,
        kpi_generator_proposal_key(settings.dna_config_id, proposal_id),
        proposal,
    )
    return proposal


def load_kpi_proposal(settings: DnaSettings, proposal_id: str) -> dict[str, Any] | None:
    return read_json_artifact(
        settings,
        kpi_generator_proposal_key(settings.dna_config_id, proposal_id),
    )


def _on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip())


def _iter_kpi_proposals(settings: DnaSettings):
    prefix = kpi_generator_proposals_prefix(settings.dna_config_id)
    for key in list_json_artifact_keys(settings, prefix):
        proposal = read_json_artifact(settings, key)
        if isinstance(proposal, dict):
            yield proposal


def load_kpi_generator_workspace(
    settings: DnaSettings,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """One S3 listing pass: latest working session, pending drafts, approved drafts."""
    working: dict[str, Any] | None = None
    working_key = ""
    pending: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []
    for proposal in _iter_kpi_proposals(settings):
        status = str(proposal.get("status") or "").strip().lower()
        if status == "working":
            sort_key = str(proposal.get("created_at") or "")
            if working is None or sort_key >= working_key:
                working = proposal
                working_key = sort_key
        elif status == "pending_review":
            pending.append(proposal)
        elif status == "approved":
            approved.append(proposal)
    pending.sort(
        key=lambda item: str(item.get("saved_at") or item.get("created_at") or ""),
        reverse=True,
    )
    approved.sort(
        key=lambda item: str(item.get("saved_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return working, pending, approved


def _write_generating_stub(
    settings: DnaSettings,
    *,
    prompt: str,
    username: str = "",
    prior_chat_history: list[dict[str, str]] | None = None,
    prior_validation_criteria: dict[str, Any] | None = None,
    prior_proposal_id: str = "",
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    proposal_id = ""
    created_at = now
    prior_id = str(prior_proposal_id or "").strip()
    prior_chat: list[dict[str, str]] = list(prior_chat_history or [])
    if prior_id:
        prior = load_kpi_proposal(settings, prior_id)
        if prior and str(prior.get("status") or "").strip().lower() == "working":
            proposal_id = prior_id
            created_at = str(prior.get("created_at") or now)
            if not prior_chat:
                prior_chat = list(prior.get("chat_history") or [])
            if prior_validation_criteria is None:
                prior_validation_criteria = validation_criteria_from_proposal(prior)
    if not proposal_id:
        proposal_id = uuid.uuid4().hex[:12]
    chat_history = _trim_kpi_chat_history(
        prior_chat + [{"role": "user", "text": prompt}]
    )
    close_working_kpi_proposals(
        settings,
        username=username,
        keep_id=proposal_id,
    )
    proposal: dict[str, Any] = {
        "proposal_id": proposal_id,
        "created_at": created_at,
        "username": username,
        "prompt": prompt,
        "chat_history": chat_history,
        "intent": "",
        "questions": [],
        "reuse": None,
        "drafts": [],
        "draft": {},
        "status": "working",
        "generation_status": GENERATION_PENDING,
    }
    if prior_validation_criteria:
        proposal["last_validation"] = dict(prior_validation_criteria)
    write_json_artifact(
        settings,
        kpi_generator_proposal_key(settings.dna_config_id, proposal_id),
        proposal,
    )
    return proposal


def _mark_generation_error(
    settings: DnaSettings,
    proposal_id: str,
    error: BaseException,
) -> dict[str, Any] | None:
    proposal = load_kpi_proposal(settings, proposal_id) or {}
    if not proposal:
        proposal = {"proposal_id": proposal_id, "status": "working"}
    proposal["generation_status"] = GENERATION_ERROR
    proposal["generation_error"] = str(error)
    write_json_artifact(
        settings,
        kpi_generator_proposal_key(settings.dna_config_id, proposal_id),
        proposal,
    )
    return proposal


def _invoke_kpi_generation_lambda(event: dict[str, Any]) -> None:
    import boto3

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if not function_name:
        raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME is not set")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
    client = boto3.client("lambda", region_name=region)
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(event, default=str).encode("utf-8"),
    )
    status = int(response.get("StatusCode") or 0)
    if status not in {202, 200}:
        raise RuntimeError(f"KPI generate invoke returned status {status}")


def enqueue_kpi_generation(
    settings: DnaSettings,
    *,
    prompt: str,
    client_id: str = "",
    monthly_budget_usd: float | None = None,
    username: str = "",
    prior_chat_history: list[dict[str, str]] | None = None,
    prior_validation_criteria: dict[str, Any] | None = None,
    prior_proposal_id: str = "",
) -> dict[str, Any]:
    """Start KPI generation. On Lambda, return immediately and finish in a worker."""
    text = (prompt or "").strip()
    if not text:
        raise ValueError("Prompt is required")
    summary = usage_summary(
        settings,
        client_id=client_id,
        monthly_budget_usd=monthly_budget_usd,
    )
    if summary.at_limit:
        raise BedrockBudgetExceeded(
            monthly_budget_usd=summary.monthly_budget_usd,
            estimated_cost_usd=summary.estimated_cost_usd,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        )
    if not _on_lambda():
        return generate_kpi_proposal(
            settings,
            prompt=text,
            client_id=client_id,
            monthly_budget_usd=monthly_budget_usd,
            username=username,
            prior_chat_history=prior_chat_history,
            prior_validation_criteria=prior_validation_criteria,
            prior_proposal_id=prior_proposal_id,
        )
    stub = _write_generating_stub(
        settings,
        prompt=text,
        username=username,
        prior_chat_history=prior_chat_history,
        prior_validation_criteria=prior_validation_criteria,
        prior_proposal_id=prior_proposal_id,
    )
    event = {
        "hiveflow_task": KPI_GENERATE_TASK,
        "proposal_id": stub["proposal_id"],
        "prompt": text,
        "client_id": client_id,
        # Explicit tenant scope so the multi-tenant worker Lambda rebinds to this
        # client's data store instead of its own (neutral) env vars.
        "company": settings.company,
        "s3_bucket": settings.s3_bucket or "",
        "monthly_budget_usd": monthly_budget_usd,
        "username": username,
        "prior_proposal_id": stub["proposal_id"],
    }
    try:
        _invoke_kpi_generation_lambda(event)
    except Exception as exc:  # noqa: BLE001
        _mark_generation_error(settings, str(stub["proposal_id"]), exc)
        raise
    return stub


def run_kpi_generation_job(settings: DnaSettings, event: dict[str, Any]) -> dict[str, Any]:
    """Lambda Event worker: Bedrock generate, write the working proposal."""
    proposal_id = str(event.get("proposal_id") or event.get("prior_proposal_id") or "").strip()
    prompt = str(event.get("prompt") or "").strip()
    username = str(event.get("username") or "").strip()
    client_id = str(event.get("client_id") or "").strip()
    budget_raw = event.get("monthly_budget_usd")
    monthly_budget_usd: float | None
    try:
        monthly_budget_usd = float(budget_raw) if budget_raw is not None else None
    except (TypeError, ValueError):
        monthly_budget_usd = None
    prior_chat_history: list[dict[str, str]] | None = None
    prior_validation_criteria = None
    if proposal_id:
        prior = load_kpi_proposal(settings, proposal_id)
        if prior and str(prior.get("status") or "").strip().lower() == "working":
            history = list(prior.get("chat_history") or [])
            if history and str(history[-1].get("role") or "") == "user":
                history = history[:-1]
            prior_chat_history = history
            prior_validation_criteria = validation_criteria_from_proposal(prior)
    try:
        proposal = generate_kpi_proposal(
            settings,
            prompt=prompt,
            client_id=client_id,
            monthly_budget_usd=monthly_budget_usd,
            username=username,
            prior_chat_history=prior_chat_history,
            prior_validation_criteria=prior_validation_criteria,
            prior_proposal_id=proposal_id,
        )
        return {
            "status": "ok",
            "proposal_id": str(proposal.get("proposal_id") or proposal_id),
        }
    except Exception as exc:  # noqa: BLE001 — Event retries would duplicate Bedrock spend
        _LOGGER.exception("KPI generation job failed for %s", proposal_id)
        if proposal_id:
            _mark_generation_error(settings, proposal_id, exc)
        return {"status": "error", "proposal_id": proposal_id, "error": str(exc)}


def find_working_kpi_proposal(settings: DnaSettings) -> dict[str, Any] | None:
    """Return the most recent working generator session, if any."""
    working, _, _ = load_kpi_generator_workspace(settings)
    return working


def close_working_kpi_proposals(
    settings: DnaSettings,
    *,
    username: str = "",
    keep_id: str = "",
) -> list[str]:
    """Discard working generator sessions so compose can reset.

    ``keep_id`` leaves one in-progress session open (used when continuing a chat).
    """
    keep = str(keep_id or "").strip()
    prefix = kpi_generator_proposals_prefix(settings.dna_config_id)
    now = datetime.now(UTC).isoformat()
    closed: list[str] = []
    for key in list_json_artifact_keys(settings, prefix):
        proposal = read_json_artifact(settings, key)
        if not isinstance(proposal, dict):
            continue
        if str(proposal.get("status") or "").strip().lower() != "working":
            continue
        pid = str(proposal.get("proposal_id") or "").strip()
        if keep and pid == keep:
            continue
        proposal["status"] = "discarded"
        proposal["discarded_at"] = now
        proposal["discarded_by"] = username
        write_json_artifact(settings, key, proposal)
        if pid:
            closed.append(pid)
    return closed


def _extract_converse_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in ((response.get("output") or {}).get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    match = _JSON_FENCE.search(raw)
    if match:
        raw = match.group(1).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Model did not return JSON") from None
        payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Model JSON must be an object")
    return payload
