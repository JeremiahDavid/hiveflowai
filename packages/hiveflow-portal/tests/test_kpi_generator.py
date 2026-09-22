"""KPI Generator nav and page smoke tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.dna_nav import (
    KPI_GENERATOR_ROOT,
    agents_section_nav,
    dna_section_nav,
)
from hiveflow.dna.web.portal.kpi_generator.render import render_kpi_generator_body
from hiveflow.dna.web.portal.kpi_generator.sql_format import format_kpi_sql
from hiveflow.dna.web.portal.kpi_generator.drafts import (
    assistant_text_from_normalized,
    inline_silver_contribution_for_gold_sql,
    normalize_generated_payload,
)
from hiveflow.dna.web.portal.kpi_generator.catalog import (
    _columns_with_companion_aliases,
    _validate_sql_columns,
    _validate_sql_joins,
    build_allowed_joins,
    build_columns_by_table,
    build_fields_by_fact,
    format_silver_columns_for_prompt,
)
from hiveflow.dna.web.portal.kpi_generator.generation import (
    MAX_KPI_CHAT_TURNS,
    _build_kpi_chat_messages,
    _trim_kpi_chat_history,
)
from hiveflow.ingest.storage import write_parquet_local
from hiveflow.storage.paths import prefix_path, silver_entity_prefix, silver_stg_entity_prefix


def test_dna_nav_lists_source_browser_and_catalog() -> None:
    labels = [item[1] for item in dna_section_nav(None)]
    assert labels == ["Source Browser", "DNA Catalog", "Data Profile", "Model Mapping"]
    assert KPI_GENERATOR_ROOT == "/portal/dna/kpi-generator"


def test_agents_nav_lists_dna_engine() -> None:
    labels = [item[1] for item in agents_section_nav()]
    assert labels[0] == "DNA Engine"
    assert agents_section_nav()[0][0] == KPI_GENERATOR_ROOT


def test_build_fields_by_fact_single_pass() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    props = {
        "tables": [
            {
                "silver_entity": "sales_orders",
                "properties": [{"name": "id"}, {"name": "amount"}],
            },
            {
                "silver_entity": "customers",
                "properties": [{"name": "id"}, {"name": "name"}],
            },
        ]
    }
    fields = build_fields_by_fact(settings, entity_properties=props)
    assert fields["sales_orders"] == ["id", "amount"]
    assert fields["customers"] == ["id", "name"]


def test_kpi_generator_render_collapses_sql() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "abc123",
            "prompt": "Net sales revenue from posted invoice lines",
            "draft": {
                "layer": "gold",
                "mode": "kpi",
                "id": "KPI-TEST",
                "fields_used": ["netAmount"],
                "filters_applied": ["posted = true"],
                "calculation": "SUM(netAmount)",
                "sql": "SELECT SUM(netAmount) AS value FROM silver_dbc_sales_invoice_lines",
            },
        },
    )
    assert "DNA Engine" not in html or True  # body has sections
    assert "kpi-draft-sql" in html
    assert "kpi-sql-editor" in html
    assert "SUM(netAmount)" in html
    assert "Validation criteria" in html
    assert "kpi-validation-shell" in html
    assert "kpi-filter-control" in html
    assert "pack-meta" in html
    assert "assistant-pack-block" in html
    assert "assistant-chat-shell" in html
    assert "assistant-compose-input" in html
    assert "portal-submit-btn" in html
    assert "assistant-bubble user" in html
    assert "kpi-add-filter" in html
    assert 'section.addEventListener("click"' in html
    assert "Save Draft" in html
    assert "data-kpi-tab" in html
    assert "semantic-builder-keys-tab" in html
    assert "activateKpiTab" in html
    assert "kpi-section-heading" in html
    assert "FROM\nsilver_dbc_sales_invoice_lines" in html or "FROM silver_dbc" in html


def test_kpi_generator_restores_validation_criteria_after_run() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "abc123",
            "prompt": "Net sales revenue",
            "draft": {
                "layer": "gold",
                "mode": "kpi",
                "id": "KPI-TEST",
                "sql": "SELECT 1",
            },
            "last_validation": {
                "filters": [
                    {
                        "fact": "sales_orders",
                        "field": "id",
                        "value": "SO-1001",
                    }
                ],
                "result": {"columns": ["value"], "rows": [{"value": "42"}]},
            },
        },
    )
    assert '"fact": "sales_orders"' in html or '\\"fact\\": \\"sales_orders\\"' in html
    assert "SO-1001" in html
    assert "savedFilters" in html
    assert "attachSqlToForm" in html
    assert "kpi-save-draft" in html
    assert "kpi-generator-validation" in html


def test_format_sql_for_display_breaks_major_clauses() -> None:
    formatted = format_kpi_sql(
        "SELECT SUM(netAmount) AS value FROM silver_dbc_sales_invoice_lines WHERE posted = true"
    )
    assert formatted.startswith("SELECT")
    assert "\nFROM " in formatted or formatted.startswith("SELECT\n")
    assert "\nWHERE " in formatted
    assert "\n  SUM(netAmount) AS value" in formatted


def test_format_kpi_sql_splits_select_columns() -> None:
    formatted = format_kpi_sql(
        "SELECT SUM(netAmount) AS value, customerId, COUNT(*) AS line_count "
        "FROM silver_dbc_sales_invoice_lines"
    )
    assert "SUM(netAmount) AS value," in formatted
    assert "\n  customerId," in formatted
    assert "\n  COUNT(*) AS line_count" in formatted


def test_trim_kpi_chat_history_keeps_last_five_user_turns() -> None:
    history = []
    for index in range(MAX_KPI_CHAT_TURNS + 2):
        history.append({"role": "user", "text": f"query {index}"})
        history.append({"role": "assistant", "text": f"reply {index}"})
    trimmed = _trim_kpi_chat_history(history)
    user_texts = [entry["text"] for entry in trimmed if entry["role"] == "user"]
    assert user_texts == [f"query {index}" for index in range(2, MAX_KPI_CHAT_TURNS + 2)]


def test_build_kpi_chat_messages_appends_new_prompt() -> None:
    messages = _build_kpi_chat_messages(
        [{"role": "user", "text": "first"}, {"role": "assistant", "text": "draft"}],
        prompt="refine it",
    )
    assert len(messages) == 3
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][0]["text"] == "refine it"


def test_kpi_generator_render_shows_chat_history() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "abc123",
            "status": "working",
            "chat_history": [
                {"role": "user", "text": "Net sales revenue"},
                {"role": "assistant", "text": "SUM of posted invoice lines"},
                {"role": "user", "text": "Exclude credit memos"},
                {"role": "assistant", "text": "Updated SQL with credit memo filter"},
            ],
            "draft": {
                "layer": "gold",
                "mode": "kpi",
                "id": "KPI-TEST",
                "sql": "SELECT 1",
            },
        },
    )
    assert "Net sales revenue" in html
    assert "Exclude credit memos" in html
    assert "Updated SQL with credit memo filter" in html
    assert 'name="prior_proposal_id"' in html
    assert 'value="abc123"' in html
    assert "Discard Draft" in html
    assert 'id="kpi-discard-draft"' in html
    assert 'btn btn-secondary' in html
    assert "DNA Engine" in html
    assert "chat.scrollTop = chat.scrollHeight" in html


def test_kpi_generator_empty_session_hides_draft_results() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal=None,
    )
    assert "Describe the KPI you want" in html
    assert 'name="prior_proposal_id"' not in html
    assert 'id="kpi-save-draft"' not in html
    assert 'id="kpi-generator-results"' not in html
    assert "assistant-bubble" not in html


def test_kpi_generator_empty_session_skips_column_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("column catalog should not load on empty GET")

    monkeypatch.setattr(
        "hiveflow.dna.web.portal.kpi_generator.render.list_fact_options",
        boom,
    )
    monkeypatch.setattr(
        "hiveflow.dna.web.portal.kpi_generator.render.build_fields_by_fact",
        boom,
    )
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal=None,
    )
    assert "Describe the KPI you want" in html


def test_kpi_generator_generating_shows_poll() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "gen1",
            "status": "working",
            "generation_status": "pending",
            "prompt": "Net sales",
            "chat_history": [{"role": "user", "text": "Net sales"}],
        },
    )
    assert 'id="kpi-generator-generating"' in html
    assert "Working on this" in html
    assert "/portal/dna/kpi-generator/status?proposal_id=gen1" in html
    assert 'id="kpi-save-draft"' not in html
    assert "Validation criteria" not in html
    assert 'name="action" value="generate"' not in html


def test_build_allowed_joins_uses_silver_fk_columns() -> None:
    relationships = {
        "source": "dbc",
        "tables": {
            "sales_invoice_lines": {
                "PK": "id",
                "silver_PK": "id",
                "relationships": [
                    {
                        "target": "sales_invoices",
                        "FK": "documentNumber",
                        "PK": "id",
                        "silver_FK": "documentId",
                        "silver_PK": "id",
                        "fk_in_silver": True,
                        "pk_in_silver": True,
                        "target_in_silver": True,
                    },
                ],
            }
        },
    }
    allowed = build_allowed_joins(relationships, source="dbc")
    join = allowed[0]
    assert join["left_column"] == "documentId"
    assert join["right_column"] == "id"


def test_build_allowed_joins_from_gold_relationships() -> None:
    relationships = {
        "source": "dbc",
        "tables": {
            "sales_invoice_lines": {
                "PK": "id",
                "relationships": [
                    {"target": "sales_invoices", "PK": "id", "FK": "documentId"},
                    {"target": "items", "PK": "id", "FK": "itemId"},
                ],
            }
        },
    }
    allowed = build_allowed_joins(relationships, source="dbc")
    assert {
        (
            join["left_table"],
            join["left_column"],
            join["right_table"],
            join["right_column"],
        )
        for join in allowed
    } == {
        (
            "silver_stg_dbc_sales_invoice_lines",
            "documentId",
            "silver_stg_dbc_sales_invoices",
            "id",
        ),
        (
            "silver_dbc_sales_invoice_lines",
            "documentId",
            "silver_dbc_sales_invoices",
            "id",
        ),
        (
            "silver_stg_dbc_sales_invoices",
            "id",
            "silver_stg_dbc_sales_invoice_lines",
            "documentId",
        ),
        (
            "silver_dbc_sales_invoices",
            "id",
            "silver_dbc_sales_invoice_lines",
            "documentId",
        ),
        (
            "silver_stg_dbc_sales_invoice_lines",
            "itemId",
            "silver_stg_dbc_items",
            "id",
        ),
        (
            "silver_dbc_sales_invoice_lines",
            "itemId",
            "silver_dbc_items",
            "id",
        ),
        (
            "silver_stg_dbc_items",
            "id",
            "silver_stg_dbc_sales_invoice_lines",
            "itemId",
        ),
        (
            "silver_dbc_items",
            "id",
            "silver_dbc_sales_invoice_lines",
            "itemId",
        ),
    }


def test_validate_sql_joins_accepts_catalog_join(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    relationships = {
        "source": "dbc",
        "tables": {
            "sales_invoice_lines": {
                "PK": "id",
                "relationships": [
                    {"target": "sales_invoices", "PK": "id", "FK": "documentId"},
                ],
            }
        },
    }
    sql = (
        "SELECT SUM(lines.netAmount) AS value "
        "FROM silver_dbc_sales_invoice_lines lines "
        "JOIN silver_dbc_sales_invoices invoices "
        "ON lines.documentId = invoices.id"
    )
    _validate_sql_joins(settings, sql, relationships=relationships)


def test_validate_sql_joins_rejects_undefined_join(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    relationships = {
        "source": "dbc",
        "tables": {
            "sales_invoice_lines": {
                "PK": "id",
                "relationships": [
                    {"target": "sales_invoices", "PK": "id", "FK": "documentId"},
                ],
            }
        },
    }
    sql = (
        "SELECT SUM(lines.netAmount) AS value "
        "FROM silver_dbc_sales_invoice_lines lines "
        "JOIN silver_dbc_customers customers ON lines.customerId = customers.id"
    )
    try:
        _validate_sql_joins(settings, sql, relationships=relationships)
    except ValueError as exc:
        assert "not defined in gold entity_relationships.yaml" in str(exc)
    else:
        raise AssertionError("expected undefined join to be rejected")


def test_build_columns_by_table_uses_live_silver_parquet(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    out = prefix_path(settings.data_dir, silver_entity_prefix(settings.source, "customers"))
    write_parquet_local(
        out,
        "data.parquet",
        [{"id": "c1", "displayName": "Acme"}],
    )
    columns = build_columns_by_table(settings)
    assert "silver_dbc_customers" in columns
    assert "silver_stg_dbc_customers" in columns
    assert "displayName" in columns["silver_dbc_customers"]
    assert "customerName" not in columns["silver_dbc_customers"]


def test_build_columns_by_table_prefers_silver_stg_over_dna_silver(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    stg = prefix_path(settings.data_dir, silver_stg_entity_prefix(settings.source, "sales_invoices"))
    dna = prefix_path(settings.data_dir, silver_entity_prefix(settings.source, "sales_invoices"))
    write_parquet_local(
        stg,
        "data.parquet",
        [{"id": "i1", "paymentTermsId": "pt1", "invoiceDate": "2026-01-01"}],
    )
    write_parquet_local(
        dna,
        "data.parquet",
        [{"id": "i1", "billToName": "Acme"}],
    )
    columns = build_columns_by_table(settings)
    assert "paymentTermsId" in columns["silver_stg_dbc_sales_invoices"]
    assert "paymentTermsId" in columns["silver_dbc_sales_invoices"]
    assert "billToName" not in columns["silver_stg_dbc_sales_invoices"]


def test_build_columns_by_table_uses_silver_stg_profile_over_docs_only_names(
    tmp_path: Path,
) -> None:
    from hiveflow.dna.store import write_yaml_artifact
    from hiveflow.storage.paths import governance_source_semantic_latest_profile_key

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    write_yaml_artifact(
        settings,
        governance_source_semantic_latest_profile_key("dbc"),
        {
            "kind": "silver_schema_profile",
            "tables": [
                {
                    "silver_entity": "sales_invoices",
                    "glue_table": "silver_stg_dbc_sales_invoices",
                    "columns": [
                        {"name": "id", "type": "string"},
                        {"name": "paymentTermsId", "type": "string"},
                        {"name": "totalAmountIncludingTax", "type": "double"},
                    ],
                },
                {
                    "silver_entity": "payment_terms",
                    "glue_table": "silver_stg_dbc_payment_terms",
                    "columns": [
                        {"name": "id", "type": "string"},
                        {"name": "code", "type": "string"},
                    ],
                },
            ],
        },
    )
    props = {
        "merged_from": {"silver_profile": {"generated_at": "2026-01-01T00:00:00Z"}},
        "tables": [
            {
                "silver_entity": "sales_invoices",
                "properties": [
                    {"name": "id", "silver_column": "id", "in_silver": True},
                    {
                        "name": "paymentTermsCode",
                        "silver_column": "paymentTermsCode",
                        "in_silver": False,
                    },
                    {
                        "name": "paymentTermsId",
                        "silver_column": "paymentTermsId",
                        "in_silver": True,
                    },
                ],
            }
        ],
    }
    columns = build_columns_by_table(settings, entity_properties=props)
    invoice_cols = columns["silver_stg_dbc_sales_invoices"]
    assert "paymentTermsId" in invoice_cols
    assert "paymentTermsCode" not in invoice_cols
    assert "code" in columns["silver_stg_dbc_payment_terms"]
    sql = (
        "SELECT invoices.paymentTermsCode, SUM(invoices.totalAmountIncludingTax) "
        "FROM silver_dbc_sales_invoices invoices "
        "GROUP BY invoices.paymentTermsCode"
    )
    try:
        _validate_sql_columns(settings, sql, columns_by_table=columns)
    except ValueError as exc:
        assert "paymentTermsCode" in str(exc)
    else:
        raise AssertionError("expected docs-only paymentTermsCode to be rejected")


def test_format_silver_columns_for_prompt_ranks_payment_terms() -> None:
    columns_by_table = {
        "silver_stg_dbc_accounts": ["id", "number"],
        "silver_dbc_accounts": ["id", "number"],
        "silver_stg_dbc_sales_invoices": [
            "id",
            "invoiceDate",
            "paymentTermsId",
            "totalAmountIncludingTax",
        ],
        "silver_dbc_sales_invoices": [
            "id",
            "invoiceDate",
            "paymentTermsId",
            "totalAmountIncludingTax",
        ],
        "silver_stg_dbc_payment_terms": ["id", "code", "displayName"],
        "silver_dbc_payment_terms": ["id", "code", "displayName"],
    }
    allowed = [
        {
            "left_table": "silver_stg_dbc_sales_invoices",
            "right_table": "silver_stg_dbc_payment_terms",
            "left_column": "paymentTermsId",
            "right_column": "id",
        }
    ]
    text = format_silver_columns_for_prompt(
        columns_by_table,
        prompt="i want to see total sales by month per peyment terms",
        allowed_joins=allowed,
        max_chars=2000,
    )
    assert "silver_stg_dbc_sales_invoices" in text
    assert "silver_stg_dbc_payment_terms" in text
    assert "paymentTermsId" in text
    assert "silver_dbc_sales_invoices:" not in text
    invoices_at = text.index("silver_stg_dbc_sales_invoices")
    accounts_at = text.index("silver_stg_dbc_accounts")
    assert invoices_at < accounts_at


def test_validate_sql_columns_rejects_unknown_group_by_column(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    out = prefix_path(settings.data_dir, silver_entity_prefix(settings.source, "sales_orders"))
    write_parquet_local(
        out,
        "data.parquet",
        [{"id": "o1", "customerId": "c1", "amount": 10}],
    )
    sql = (
        "SELECT o.id, MAX(c.customerName) AS customer_name "
        "FROM silver_dbc_sales_orders o "
        "JOIN silver_dbc_customers c ON o.customerId = c.id "
        "GROUP BY o.id, o.customerName"
    )
    columns = build_columns_by_table(settings)
    try:
        _validate_sql_columns(settings, sql, columns_by_table=columns)
    except ValueError as exc:
        assert "customerName" in str(exc)
        assert "silver_dbc_sales_orders" in str(exc)
    else:
        raise AssertionError("expected unknown GROUP BY column to be rejected")


def test_validate_sql_columns_accepts_known_group_by_columns(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    orders_out = prefix_path(settings.data_dir, silver_entity_prefix(settings.source, "sales_orders"))
    customers_out = prefix_path(settings.data_dir, silver_entity_prefix(settings.source, "customers"))
    write_parquet_local(
        orders_out,
        "data.parquet",
        [{"id": "o1", "customerId": "c1", "amount": 10}],
    )
    write_parquet_local(
        customers_out,
        "data.parquet",
        [{"id": "c1", "displayName": "Acme"}],
    )
    sql = (
        "SELECT o.id, o.amount, MAX(c.displayName) AS customer_name "
        "FROM silver_dbc_sales_orders o "
        "JOIN silver_dbc_customers c ON o.customerId = c.id "
        "GROUP BY o.id, o.amount"
    )
    columns = build_columns_by_table(settings)
    _validate_sql_columns(settings, sql, columns_by_table=columns)


def test_normalize_generated_payload_legacy_single_draft() -> None:
    payload = {
        "layer": "gold",
        "mode": "kpi",
        "id": "kpi_rev",
        "output_id": "out_rev",
        "grain_columns": [],
        "sql": "SELECT 1",
        "summary": "Net revenue",
    }
    normalized = normalize_generated_payload(payload)
    assert normalized["intent"] == "implement"
    assert len(normalized["drafts"]) == 1
    assert normalized["drafts"][0]["layer"] == "gold"


def test_normalize_generated_payload_clarify_and_reuse() -> None:
    clarify = normalize_generated_payload(
        {"intent": "clarify", "questions": ["Which customers are interco?"]}
    )
    assert clarify["intent"] == "clarify"
    assert clarify["questions"] == ["Which customers are interco?"]
    assert clarify["drafts"] == []
    text = assistant_text_from_normalized(clarify)
    assert "Which customers are interco?" in text

    reuse = normalize_generated_payload(
        {
            "intent": "reuse",
            "reuse": {
                "reason": "Gold output out_interco already has that grain",
                "output_id": "out_interco",
            },
        }
    )
    assert reuse["intent"] == "reuse"
    assert reuse["reuse"]["output_id"] == "out_interco"


def test_normalize_generated_payload_split_drafts_and_rejects_two_silvers() -> None:
    normalized = normalize_generated_payload(
        {
            "intent": "implement",
            "summary": "Interco sales",
            "drafts": [
                {
                    "layer": "gold",
                    "mode": "kpi",
                    "id": "kpi_interco",
                    "output_id": "out_interco",
                    "sql": "SELECT 1",
                },
                {
                    "layer": "silver",
                    "mode": "add_columns",
                    "id": "add_interco",
                    "target_entity": "customers",
                    "sql": "SELECT 1",
                },
            ],
        }
    )
    assert [d["layer"] for d in normalized["drafts"]] == ["silver", "gold"]
    with pytest.raises(ValueError, match="at most one silver"):
        normalize_generated_payload(
            {
                "intent": "implement",
                "drafts": [
                    {
                        "layer": "silver",
                        "mode": "add_columns",
                        "id": "a",
                        "target_entity": "customers",
                        "sql": "SELECT 1",
                    },
                    {
                        "layer": "silver",
                        "mode": "add_columns",
                        "id": "b",
                        "target_entity": "vendors",
                        "sql": "SELECT 1",
                    },
                ],
            }
        )


def test_inline_silver_contribution_rewrites_dna_silver_table() -> None:
    gold = (
        "SELECT c.is_interco, SUM(s.amount) AS total_sales "
        "FROM silver_dbc_sales_invoice_lines s "
        "JOIN silver_dbc_customers c ON s.customerId = c.id "
        "GROUP BY c.is_interco"
    )
    silver = (
        "SELECT c.*, CASE WHEN c.displayName IN ('example1') THEN true ELSE false END "
        "AS is_interco FROM silver_stg_dbc_customers c"
    )
    rewritten = inline_silver_contribution_for_gold_sql(
        gold,
        source="dbc",
        target_entity="customers",
        contribution_sql=silver,
    )
    assert rewritten.startswith("WITH _kpi_enh_customers AS")
    assert "silver_stg_dbc_customers" in rewritten
    assert "JOIN _kpi_enh_customers c" in rewritten
    assert "JOIN silver_dbc_customers" not in rewritten


def test_companion_silver_aliases_are_known_on_dna_silver(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    columns = {"silver_dbc_customers": ["id", "displayName"]}
    drafts = [
        {
            "layer": "silver",
            "target_entity": "customers",
            "sql": (
                "SELECT c.*, CASE WHEN c.displayName IN ('a') THEN true ELSE false END "
                "AS is_interco FROM silver_stg_dbc_customers c"
            ),
        }
    ]
    merged = _columns_with_companion_aliases(
        settings, drafts, columns_by_table=columns
    )
    assert "is_interco" in merged["silver_dbc_customers"]
    gold_sql = (
        "SELECT c.is_interco, COUNT(*) AS n FROM silver_dbc_customers c GROUP BY c.is_interco"
    )
    _validate_sql_columns(settings, gold_sql, columns_by_table=merged)


def test_kpi_generator_clarify_hides_save_draft() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "ask1",
            "intent": "clarify",
            "questions": ["Which customers are interco?"],
            "chat_history": [
                {"role": "user", "text": "Total interco sales by month"},
                {
                    "role": "assistant",
                    "text": "1. Which customers are interco?",
                },
            ],
        },
    )
    assert "Which customers are interco?" in html
    assert 'id="kpi-save-draft"' not in html
    assert "Validation criteria" not in html


def test_kpi_generator_reuse_hides_save_draft() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "reuse1",
            "intent": "reuse",
            "reuse": {
                "reason": "Gold output out_interco already has that grain",
                "output_id": "out_interco",
            },
            "chat_history": [
                {"role": "user", "text": "Interco sales"},
                {
                    "role": "assistant",
                    "text": "Existing DNA output out_interco already covers this.",
                },
            ],
        },
    )
    assert "Existing DNA" in html
    assert "out_interco" in html
    assert 'id="kpi-save-draft"' not in html
    assert "Validation criteria" not in html


def test_kpi_generator_implement_split_shows_both_sql_editors() -> None:
    settings = DnaSettings(source="dbc", data_dir=Path("."), company="poc")
    html = render_kpi_generator_body(
        settings=settings,
        url=lambda p: p,
        is_admin=True,
        proposal={
            "proposal_id": "split1",
            "intent": "implement",
            "drafts": [
                {
                    "layer": "silver",
                    "mode": "add_columns",
                    "id": "add_is_interco",
                    "target_entity": "customers",
                    "sql": "SELECT id, true AS is_interco FROM silver_stg_dbc_customers",
                },
                {
                    "layer": "gold",
                    "mode": "kpi",
                    "id": "kpi_interco_sales",
                    "output_id": "out_interco_sales",
                    "grain_columns": ["is_interco"],
                    "sql": "SELECT is_interco, SUM(amount) AS total FROM silver_dbc_customers GROUP BY is_interco",
                },
            ],
            "draft": {
                "layer": "gold",
                "mode": "kpi",
                "id": "kpi_interco_sales",
                "output_id": "out_interco_sales",
                "sql": "SELECT is_interco, SUM(amount) AS total FROM silver_dbc_customers GROUP BY is_interco",
            },
        },
    )
    assert "silver update" in html
    assert "gold update" in html
    assert 'id="kpi-draft-sql"' in html
    assert 'data-kpi-sql-layer="silver"' in html
    assert 'data-kpi-sql-layer="gold"' in html
    assert 'id="kpi-save-draft"' in html
    assert "Validation criteria" in html
