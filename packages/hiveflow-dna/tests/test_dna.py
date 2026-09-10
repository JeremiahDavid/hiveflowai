from __future__ import annotations

from pathlib import Path

import pytest

from hiveflow.dna.compile import compile_pack
from hiveflow.dna.schema import load_definition_pack_file, starter_pack_path
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.validate import run_validation
from hiveflow.dna.workflow import promote_pack
from hiveflow.project_config import (
    dna_catalog_table_name,
    dna_stack_name,
    get_dna_config,
    get_ui_config,
    get_ui_domain_config,
    is_dna_stack_enabled,
    is_ui_domain_enabled,
    iter_dna_catalog_outputs,
    resolve_dna_source,
    ui_stack_name,
)
from hiveflow.storage.paths import gold_dna_entity_parquet_key, gold_dna_prefix

def test_starter_pack_loads() -> None:
    pack = load_definition_pack_file(starter_pack_path())
    assert pack.pack_id == "bc_intra_v1"
    assert pack.is_publishable()
    assert len(pack.kpis) >= 5
    assert pack.calendar is not None
    assert pack.calendar.date_column == "postingDate"
    yoy = pack.kpi_by_id("KPI-REV-01-YoY")
    assert yoy.formula_type == "period_compare"
    assert yoy.group_by == ["customerId"]
    assert yoy.format is not None
    assert yoy.format.scale == "thousands"
    assert pack.output_by_id("out_rev_by_customer_period").kpi_ids == ["KPI-REV-01-YoY"]

def test_calendar_period_attrs() -> None:
    from hiveflow.dna.calendar import period_attrs_for_date
    from datetime import date

    jan = period_attrs_for_date(date(2026, 1, 15), fiscal_year_start_month=1)
    assert jan.period_key == "FY2026-P01"
    assert jan.prior_year_period_key == "FY2025-P01"

    apr_fiscal = period_attrs_for_date(date(2026, 4, 15), fiscal_year_start_month=4)
    assert apr_fiscal.fiscal_year == 2027
    assert apr_fiscal.fiscal_period == 1
    assert apr_fiscal.period_key == "FY2027-P01"
    assert apr_fiscal.prior_year_period_key == "FY2026-P01"

    mar_fiscal = period_attrs_for_date(date(2026, 3, 1), fiscal_year_start_month=4)
    assert mar_fiscal.fiscal_year == 2026
    assert mar_fiscal.fiscal_period == 12

def test_compile_customer_yoy_with_silver(tmp_path: Path) -> None:
    from hiveflow.dna.store import read_staging_output
    from hiveflow.ingest.storage import write_parquet_local
    from hiveflow.storage.paths import prefix_path, silver_entity_prefix

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    pack = load_definition_pack_file(starter_pack_path())

    invoices = [
        {
            "id": "INV1",
            "customerId": "C1",
            "postingDate": "2026-01-10",
            "customerNumber": "100",
            "customerName": "Acme",
        },
        {
            "id": "INV2",
            "customerId": "C1",
            "postingDate": "2025-01-12",
            "customerNumber": "100",
            "customerName": "Acme",
        },
        {
            "id": "INV3",
            "customerId": "C2",
            "postingDate": "2026-01-20",
            "customerNumber": "200",
            "customerName": "Beta",
        },
    ]
    lines = [
        {
            "id": "L1",
            "documentId": "INV1",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 1,
            "unitPrice": 100,
            "netAmount": 100,
        },
        {
            "id": "L2",
            "documentId": "INV2",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 1,
            "unitPrice": 80,
            "netAmount": 80,
        },
        {
            "id": "L3",
            "documentId": "INV3",
            "sequence": 1,
            "customerId": "C2",
            "itemId": "I2",
            "quantity": 2,
            "unitPrice": 50,
            "netAmount": 50,
        },
    ]
    for entity, rows in (
        ("sales_invoices", invoices),
        ("sales_invoice_lines", lines),
        ("customers", []),
        ("items", []),
        ("sales_order_lines", []),
        ("sales_shipment_lines", []),
    ):
        out_dir = prefix_path(tmp_path, silver_entity_prefix(settings.source, entity))
        write_parquet_local(out_dir, "data.parquet", rows)

    manifest = compile_pack(settings, pack)
    assert manifest["status"] == "compiled"

    fact = read_staging_output(settings, "out_fact_revenue_lines")
    assert fact
    assert "period_key" in fact[0]
    assert any(row.get("period_key") == "FY2026-P01" for row in fact)

    yoy = read_staging_output(settings, "out_rev_by_customer_period")
    assert yoy
    c1_2026 = next(
        row
        for row in yoy
        if row.get("customerId") == "C1" and row.get("period_key") == "FY2026-P01"
    )
    assert c1_2026["value_cy"] == 100.0
    assert c1_2026["value_py"] == 80.0
    assert c1_2026["delta"] == 20.0
    assert c1_2026["pct_change"] == 0.25
    assert c1_2026["format_scale"] == "thousands"
    assert c1_2026["format_type"] == "currency"

    validation = run_validation(settings, pack)
    assert validation["status"] == "passed"

def test_compile_validate_empty_silver(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    pack = load_definition_pack_file(starter_pack_path())
    manifest = compile_pack(settings, pack)
    assert manifest["status"] == "compiled"
    assert manifest["pack_id"] == "bc_intra_v1"

    validation = run_validation(settings, pack)
    assert validation["status"] == "passed"


def test_promote_workflow(tmp_path: Path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="test_pack")
    pack = load_definition_pack_file(starter_pack_path())
    pack.pack_id = "test_pack"
    pack.version = "0.1.0"
    pack.approval.status = "draft"
    result = promote_pack(settings, pack, target_status="validated", approver="Controller")
    assert result["status"] == "validated"
    assert pack.is_publishable()
    assert "governance" in str(result["pack_path"]).replace("\\", "/")
    assert (tmp_path / "governance" / "test_pack" / "v0.1.0" / "test_pack.yaml").is_file()


def test_promote_to_production_sets_active_version(tmp_path: Path) -> None:
    from hiveflow.dna.reporting import load_production_reporting
    from hiveflow.dna.workflow import load_production_pack, load_workflow_state

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="ACME")
    pack = load_definition_pack_file(starter_pack_path())
    pack.pack_id = "acme_dna_config"
    pack.version = "0.1.0"
    pack.approval.status = "draft"
    promote_pack(settings, pack, target_status="validated", approver="Controller")
    promote_pack(settings, pack, target_status="production", approver="Controller")
    state = load_workflow_state(settings, "acme_dna_config")
    assert state["active_version"] == "0.1.0"
    production = load_production_pack(settings)
    assert production.pack_id == "acme_dna_config"
    reporting = load_production_reporting(settings)
    assert reporting is not None


def test_save_governance_version_with_docs(tmp_path: Path) -> None:
    from hiveflow.dna.governance import (
        load_governance_dna,
        load_governance_doc,
        load_governance_manifest,
        save_governance_version,
    )
    from hiveflow.dna.reporting import (
        default_reporting_pack,
        load_reporting_pack_from_governance,
    )

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    pack = load_definition_pack_file(starter_pack_path())
    pack.pack_id = settings.pack_id
    saved = save_governance_version(
        settings,
        pack=pack,
        reporting=default_reporting_pack(
            pack_id="poc_reporting_config",
            version=pack.version,
            status=pack.approval.status,
        ),
        docs=[
            {
                "title": "Revenue definition",
                "filename": "revenue-definition.md",
                "content": "# Revenue\n\nSum of invoice lines.",
            }
        ],
    )
    assert saved["version"] == pack.version
    assert saved["dna_path"].replace("\\", "/").endswith("poc_dna_config.yaml")
    loaded = load_governance_dna(settings, pack.pack_id, pack.version)
    assert loaded.pack_id == pack.pack_id
    assert loaded.version == pack.version

    reporting = load_reporting_pack_from_governance(settings, pack.pack_id, pack.version)
    assert reporting["pack_id"] == "poc_reporting_config"

    manifest = load_governance_manifest(settings, pack.pack_id, pack.version)
    assert manifest is not None
    assert manifest["artifacts"]["dna"]["key"].endswith("/poc_dna_config.yaml")
    assert "reporting" in manifest["artifacts"]

    doc_text = load_governance_doc(
        settings, pack.pack_id, pack.version, "revenue-definition.md"
    )
    assert doc_text is not None
    assert "Sum of invoice lines" in doc_text

def test_init_client_governance_seeds_boilerplates(tmp_path: Path) -> None:
    from hiveflow.dna.governance import governance_pack_exists
    from hiveflow.dna.init_client import init_client_governance
    from hiveflow.dna.reporting import load_production_reporting
    from hiveflow.dna.workflow import load_production_pack

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    assert settings.pack_id == "poc_dna_config"
    assert governance_pack_exists(settings, settings.pack_id) is False

    first = init_client_governance(settings, company="POC")
    assert first["status"] == "initialized"
    assert first["dna_config"] == "poc_dna_config.yaml"
    assert (tmp_path / "governance" / "poc_dna_config" / "workflow.json").is_file()
    assert (
        tmp_path / "governance" / "poc_dna_config" / "v1.1.0" / "poc_dna_config.yaml"
    ).is_file()
    assert (
        tmp_path
        / "governance"
        / "poc_dna_config"
        / "v1.1.0"
        / "poc_reporting_config.yaml"
    ).is_file()

    pack = load_production_pack(settings)
    assert pack.pack_id == "poc_dna_config"
    assert settings.reporting_config_id == "poc_reporting_config"
    assert first["reporting_config"] == "poc_reporting_config.yaml"

    reporting = load_production_reporting(settings)
    assert reporting["pack_id"] == "poc_reporting_config"
    assert reporting["pages"]
    assert any(page.get("id") == "page_executive" for page in reporting["pages"])
    assert any(page.get("pillar") == "executive" for page in reporting["pages"])

    second = init_client_governance(settings, company="POC")
    assert second["status"] == "skipped"
    assert second["reason"] == "governance_pack_exists"

def test_compile_executive_kpis_with_silver(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from unittest.mock import patch

    from hiveflow.dna.store import read_staging_output
    from hiveflow.ingest.storage import write_parquet_local
    from hiveflow.storage.paths import prefix_path, silver_entity_prefix

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    pack = load_definition_pack_file(starter_pack_path())

    invoices = [
        {
            "id": "INV1",
            "customerId": "C1",
            "postingDate": "2026-01-10",
            "customerNumber": "100",
            "customerName": "Acme",
        },
        {
            "id": "INV2",
            "customerId": "C1",
            "postingDate": "2025-01-12",
            "customerNumber": "100",
            "customerName": "Acme",
        },
    ]
    lines = [
        {
            "id": "L1",
            "documentId": "INV1",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 2,
            "unitPrice": 100,
            "unitCost": 40,
            "netAmount": 200,
        },
        {
            "id": "L2",
            "documentId": "INV2",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 1,
            "unitPrice": 80,
            "unitCost": 30,
            "netAmount": 80,
        },
    ]
    orders = [
        {
            "id": "SO1",
            "customerId": "C1",
            "orderDate": "2026-01-05",
            "status": "Open",
            "customerNumber": "100",
            "customerName": "Acme",
        },
    ]
    order_lines = [
        {
            "id": "OL1",
            "documentId": "SO1",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 5,
            "unitPrice": 90,
            "outstandingQuantity": 3,
        },
    ]
    for entity, rows in (
        ("sales_invoices", invoices),
        ("sales_invoice_lines", lines),
        ("sales_orders", orders),
        ("sales_order_lines", order_lines),
        ("customers", [{"id": "C1", "number": "100", "displayName": "Acme"}]),
        ("items", [{"id": "I1", "number": "ITEM1", "displayName": "Widget"}]),
        ("sales_shipment_lines", []),
    ):
        out_dir = prefix_path(tmp_path, silver_entity_prefix(settings.source, entity))
        write_parquet_local(out_dir, "data.parquet", rows)

    frozen = datetime(2026, 1, 15, tzinfo=UTC)
    with patch("hiveflow.dna.compile.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        mock_dt.fromisoformat = datetime.fromisoformat
        manifest = compile_pack(settings, pack)
    assert manifest["status"] == "compiled"

    fact = read_staging_output(settings, "out_fact_revenue_lines")
    assert fact
    row = fact[0]
    assert row.get("grossProfit") == 120.0
    assert row.get("costAmount") == 80.0

    executive = read_staging_output(settings, "out_executive_kpis")
    assert executive
    rev_mtd = next(row for row in executive if row.get("kpi_id") == "KPI-REV-YoY-MTD")
    assert rev_mtd["window"] == "mtd"
    assert rev_mtd["value_cy"] == 200.0
    assert rev_mtd["value_py"] == 80.0

    snapshot = read_staging_output(settings, "out_executive_snapshot")
    backlog = next(row for row in snapshot if row.get("kpi_id") == "KPI-BKL-01")
    assert backlog["value"] == 270.0

    top_customers = read_staging_output(settings, "out_top_customers_ytd")
    assert top_customers
    assert top_customers[0]["customerId"] == "C1"

def test_compile_executive_kpis_carry_forward_quiet_month(tmp_path: Path) -> None:
    """YTD/QTD must still publish when as-of is after the last activity month."""
    from datetime import UTC, datetime
    from unittest.mock import patch

    from hiveflow.dna.store import read_staging_output
    from hiveflow.ingest.storage import write_parquet_local
    from hiveflow.storage.paths import prefix_path, silver_entity_prefix

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    pack = load_definition_pack_file(starter_pack_path())

    invoices = [
        {
            "id": "INV1",
            "customerId": "C1",
            "postingDate": "2026-07-01",
            "customerNumber": "100",
            "customerName": "Acme",
        },
        {
            "id": "INV2",
            "customerId": "C1",
            "postingDate": "2025-07-15",
            "customerNumber": "100",
            "customerName": "Acme",
        },
        {
            "id": "INV3",
            "customerId": "C1",
            "postingDate": "2025-08-10",
            "customerNumber": "100",
            "customerName": "Acme",
        },
    ]
    lines = [
        {
            "id": "L1",
            "documentId": "INV1",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 2,
            "unitPrice": 100,
            "unitCost": 40,
            "netAmount": 200,
        },
        {
            "id": "L2",
            "documentId": "INV2",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 1,
            "unitPrice": 50,
            "unitCost": 20,
            "netAmount": 50,
        },
        {
            "id": "L3",
            "documentId": "INV3",
            "sequence": 1,
            "customerId": "C1",
            "itemId": "I1",
            "quantity": 1,
            "unitPrice": 80,
            "unitCost": 30,
            "netAmount": 80,
        },
    ]
    for entity, rows in (
        ("sales_invoices", invoices),
        ("sales_invoice_lines", lines),
        ("sales_orders", []),
        ("sales_order_lines", []),
        ("customers", [{"id": "C1", "number": "100", "displayName": "Acme"}]),
        ("items", [{"id": "I1", "number": "ITEM1", "displayName": "Widget"}]),
        ("sales_shipment_lines", []),
    ):
        out_dir = prefix_path(tmp_path, silver_entity_prefix(settings.source, entity))
        write_parquet_local(out_dir, "data.parquet", rows)

    # Last CY activity is July; as-of is August (quiet month) — mirrors POC gold.
    frozen = datetime(2026, 8, 5, tzinfo=UTC)
    with patch("hiveflow.dna.compile.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        mock_dt.fromisoformat = datetime.fromisoformat
        manifest = compile_pack(settings, pack)
    assert manifest["status"] == "compiled"

    executive = read_staging_output(settings, "out_executive_kpis")
    assert executive
    rev_mtd = next(row for row in executive if row.get("kpi_id") == "KPI-REV-YoY-MTD")
    assert rev_mtd["period_key"] == "FY2026-P08"
    assert rev_mtd["value_cy"] == 0.0
    assert rev_mtd["value_py"] == 80.0

    rev_ytd = next(row for row in executive if row.get("kpi_id") == "KPI-REV-YoY-YTD")
    assert rev_ytd["period_key"] == "FY2026-P08"
    assert rev_ytd["value_cy"] == 200.0
    # Prior YTD through August includes July + August 2025.
    assert rev_ytd["value_py"] == 130.0

    rev_qtd = next(row for row in executive if row.get("kpi_id") == "KPI-REV-YoY-QTD")
    assert rev_qtd["period_key"] == "FY2026-P08"
    assert rev_qtd["value_cy"] == 200.0
    assert rev_qtd["value_py"] == 130.0

    top_customers = read_staging_output(settings, "out_top_customers_ytd")
    assert top_customers
    assert top_customers[0]["customerId"] == "C1"
    assert top_customers[0]["value_cy"] == 200.0

def test_governance_legacy_definition_pack_fallback(tmp_path: Path) -> None:
    from hiveflow.dna.governance import load_governance_dna
    from hiveflow.dna.store import write_json_artifact

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="legacy_pack")
    pack = load_definition_pack_file(starter_pack_path())
    pack.pack_id = "legacy_pack"
    pack.version = "9.9.9"
    # Write only to legacy path — governance loader should still find it.
    write_json_artifact(
        settings,
        f"dna/definition_packs/v{pack.version}/{pack.pack_id}.json",
        pack.to_dict(),
    )
    loaded = load_governance_dna(settings, pack.pack_id, pack.version)
    assert loaded.pack_id == "legacy_pack"
    assert loaded.version == "9.9.9"

def test_ensure_reporting_config_seeds_when_sidecar_missing(tmp_path: Path) -> None:
    from hiveflow.dna.governance import save_governance_version
    from hiveflow.dna.init_client import ensure_reporting_config
    from hiveflow.dna.schema import load_definition_pack_file
    from hiveflow.dna.store import write_json_artifact
    from hiveflow.dna.reporting import load_production_reporting
    from hiveflow.storage.paths import governance_workflow_key

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    pack = load_definition_pack_file(starter_pack_path())
    pack.pack_id = "poc_dna_config"
    pack.version = "1.0.0"
    pack.status = "production"
    pack.approval.status = "production"
    # DNA + workflow only — no reporting sidecar.
    save_governance_version(settings, pack=pack, reporting=None)
    write_json_artifact(
        settings,
        governance_workflow_key("poc_dna_config"),
        {
            "pack_id": "poc_dna_config",
            "company": "POC",
            "active_version": "1.0.0",
            "history": [],
        },
    )

    result = ensure_reporting_config(settings)
    assert result["status"] == "initialized"
    assert result["reporting_config"] == "poc_reporting_config.yaml"
    assert (
        tmp_path
        / "governance"
        / "poc_dna_config"
        / "v1.0.0"
        / "poc_reporting_config.yaml"
    ).is_file()
    reporting = load_production_reporting(settings)
    assert reporting["pack_id"] == "poc_reporting_config"
    assert any(page.get("path") == "/portal/executive" for page in reporting["pages"])

    skipped = ensure_reporting_config(settings)
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "reporting_config_exists"

def test_dna_catalog_table_naming() -> None:
    assert dna_catalog_table_name("out_fact_revenue_lines") == "dna_out_fact_revenue_lines"
    assert "out_kpi_snapshot" in iter_dna_catalog_outputs()

def test_gold_dna_paths() -> None:
    assert gold_dna_prefix() == "gold/dna"
    assert gold_dna_entity_parquet_key("out_kpi_snapshot") == "gold/dna/out_kpi_snapshot/data.parquet"

def test_dna_stack_gating_from_config() -> None:
    assert is_dna_stack_enabled({"dna": {"enabled": True}})
    assert not is_dna_stack_enabled({"dna": {"enabled": False}})
    assert not is_dna_stack_enabled({})
    assert dna_stack_name("POC", "dev") == "DnaStack-POC-dev"
    assert resolve_dna_source({"dbc": {}, "dna": {}}) == "dbc"

def test_ui_stack_gating_from_config() -> None:
    enabled_env = {"dna": {"enabled": True}, "ui": {"enabled": True}}
    assert get_ui_config(enabled_env)["enabled"] is True
    assert ui_stack_name("POC", "dev") == "UiStack-POC-dev"

def test_platform_stack_names() -> None:
    from hiveflow.project_config import (
        get_platform_environment_config,
        global_dns_stack_name,
        global_ui_stack_name,
        is_platform_ui_enabled,
        reporting_stack_name,
        resolve_reporting_site_url,
    )

    assert global_ui_stack_name("dev") == "GlobalUiStack-dev"
    assert global_dns_stack_name("dev") == "GlobalDnsStack-dev"
    assert reporting_stack_name("poc", "dev") == "ReportingStack-poc-dev"
    from hiveflow.project_config import global_ui_web_api_export_name, reporting_web_api_export_name

    assert global_ui_web_api_export_name("dev") == "hiveflow-global-ui-dev-web-api-id"
    assert reporting_web_api_export_name("poc", "dev") == "hiveflow-reporting-poc-dev-web-api-id"
    platform_env = get_platform_environment_config("dev")
    assert is_platform_ui_enabled(platform_env)
    assert resolve_reporting_site_url(
        {"zone_name": "hive-flow-ai.com"},
        {"reporting_hostname": "poc"},
        "poc",
    ) == "https://poc.hive-flow-ai.com/"

def test_ui_domain_config_from_yaml() -> None:
    full_env = {
        "ui": {
            "domain": {
                "zone_name": "hive-flow-ai.com",
                "primary_hostname": "hive-flow-ai.com",
                "alternate_hostnames": ["www"],
            }
        }
    }
    assert is_ui_domain_enabled(full_env)
    domain_cfg = get_ui_domain_config(full_env)
    assert domain_cfg["zone_name"] == "hive-flow-ai.com"
    assert domain_cfg["alternate_hostnames"] == ["www"]

def test_ui_dns_not_managed_by_default_when_zone_imported() -> None:
    from hiveflow.project_config import is_ui_dns_managed, resolve_ui_primary_site_url

    env_config = {
        "ui": {
            "domain": {
                "zone_name": "hive-flow-ai.com",
                "primary_hostname": "hive-flow-ai.com",
                "hosted_zone_id": "Z1234567890ABC",
                "manage_dns": False,
            }
        }
    }
    assert is_ui_dns_managed(env_config) is False
    assert resolve_ui_primary_site_url(env_config) == "https://hive-flow-ai.com/"

def test_ui_dns_managed_only_for_explicit_bootstrap() -> None:
    from hiveflow.project_config import is_ui_dns_managed

    assert is_ui_dns_managed({"ui": {"domain": {"manage_dns": True, "zone_name": "hive-flow-ai.com"}}}) is True
    assert is_ui_dns_managed({"ui": {"domain": {"create_hosted_zone": True, "zone_name": "hive-flow-ai.com"}}}) is True
    assert is_ui_dns_managed({"ui": {"domain": {"hosted_zone_id": "Z123", "zone_name": "hive-flow-ai.com"}}}) is False

def test_global_dns_stack_enabled_after_bootstrap() -> None:
    from hiveflow.project_config import is_global_dns_stack_enabled, is_ui_dns_managed

    env_config = {
        "ui": {
            "domain": {
                "zone_name": "hive-flow-ai.com",
                "manage_dns": False,
                "hosted_zone_id": "Z0833907O664KG7NO3CQ",
            }
        }
    }
    assert is_ui_dns_managed(env_config) is False
    assert is_global_dns_stack_enabled(env_config) is True


def test_deploy_stack_names_onboards_data_plane_only() -> None:
    from hiveflow.project_config import deploy_stack_names

    # Onboarding provisions only the client data plane now; the shared
    # PortalStack + wildcard DNS are environment-level, not per client.
    assert deploy_stack_names(
        company="poc2", environment="dev", client_id="poc2", scope="platform"
    ) == []
    assert deploy_stack_names(
        company="poc2", environment="dev", client_id="poc2", scope="all"
    ) == ["IngestStack-POC2-dev", "DnaStack-POC2-dev"]

def test_resolve_dna_settings_global_ui_skips_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    from hiveflow.dna.runtime import resolve_dna_settings

    monkeypatch.setenv("HIVEFLOW_UI_MODE", "global")
    monkeypatch.setenv("HIVEFLOW_ENVIRONMENT", "dev")
    monkeypatch.delenv("HIVEFLOW_S3_BUCKET", raising=False)
    monkeypatch.delenv("HIVEFLOW_PLATFORM_UI", raising=False)

    settings = resolve_dna_settings()
    assert settings.s3_bucket is None
    assert settings.pack_id.endswith("_dna_config")
    assert settings.company
