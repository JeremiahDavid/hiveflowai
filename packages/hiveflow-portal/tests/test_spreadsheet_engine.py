"""Portal tests for Spreadsheet Engine Source Browser UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.test import Client

from hiveflow.dna.init_client import init_client_governance
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import create_app
from hiveflow.project_config import load_project_config


@pytest.fixture
def portal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")


def _client(tmp_path: Path) -> Client:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    init_client_governance(settings, company="POC")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    return Client(
        create_app(
            settings,
            company="POC",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting",
        )
    )


def test_source_browser_lists_spreadsheet_engine_first(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/semantics/source-docs")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Spreadsheet Engine" in html
    assert html.index("Spreadsheet Engine") < html.index("Business Central")


def test_spreadsheet_engine_route_renders_upload(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/semantics/source-docs/sse")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Upload workbook" in html
    assert "Proposals" in html
    assert "semantic-builder-keys-tabs" in html
    assert 'id="spreadsheet-table-chat"' not in html


def test_state_machine_arn_uses_sts_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVEFLOW_SPREADSHEET_STATE_MACHINE_ARN", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-2")

    class _Sts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    class _Boto3:
        def client(self, name: str):
            assert name == "sts"
            return _Sts()

    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto3())

    from hiveflow.dna.web.portal.spreadsheet_engine.service import _state_machine_arn

    arn = _state_machine_arn(company="POC", environment="dev")
    assert arn == "arn:aws:states:us-east-2:123456789012:stateMachine:poc-dev-spreadsheet"


def test_proposal_review_renders_table_preview() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    job = {"job_id": "job1", "status": "ready", "filename": "sample.xlsx"}
    table = {
        "table_id": "t0",
        "entity_name": "customers",
        "purpose": "Customer master",
        "grain": "one row per customer",
        "confidence": 0.9,
        "status": "pending_review",
        "schema": [
            {"name": "customer_id", "type": "string", "description": "id", "is_key": True},
            {"name": "company", "type": "string", "description": "name"},
        ],
        "profiling": {
            "columns": [
                {
                    "name": "customer_id",
                    "inferred_type": "string",
                    "null_rate": 0,
                    "cardinality": 2,
                    "likely_key": True,
                    "patterns": [],
                },
                {
                    "name": "company",
                    "inferred_type": "string",
                    "null_rate": 0,
                    "cardinality": 2,
                    "likely_key": False,
                    "patterns": [],
                },
            ]
        },
        "source": {"sheet": "Customers", "row_count": 2},
    }
    preview = {
        "headers": ["customer_id", "company"],
        "rows": [["C1", "Acme"], ["C2", "Beta"]],
        "row_count": 2,
        "preview_row_count": 2,
        "truncated": False,
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job=job,
        report={"tables": [table]},
        active_tab="review",
        table_preview=preview,
    )
    assert "Source data preview" in html
    assert "Showing 2 of 2 data rows." in html
    assert "is-condensed" in html
    assert "spreadsheet-schema-toggle" in html
    assert "Proposed schema" in html
    assert "Column profiling" in html
    assert "Acme" in html


def test_notes_section_renders_cleanly() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import _table_analysis_html

    html = _table_analysis_html(
        {
            "table_id": "t0",
            "entity_name": "customers",
            "purpose": "Customer master",
            "grain": "one row per customer",
            "confidence": 0.9,
            "status": "pending_review",
            "schema": [{"name": "customer_id", "type": "string", "description": "id"}],
            "profiling": {
                "columns": [
                    {
                        "name": "customer_id",
                        "inferred_type": "string",
                        "null_rate": 0,
                        "cardinality": 2,
                        "likely_key": True,
                        "patterns": [],
                    }
                ]
            },
            "notes": [
                "Heuristic fallback — Bedrock unavailable or returned invalid JSON.",
                "Rows alternate between item header and price detail.",
            ],
        },
        embedded=True,
    )
    assert "spreadsheet-notes" in html
    assert "spreadsheet-step-notes" in html
    assert "Schema inferred locally" in html
    assert "Rows alternate between item header and price detail." in html
    assert "Heuristic fallback" not in html
    assert "spreadsheet-schema-toggle" in html


def test_proposal_review_renders_table_chat(tmp_path: Path, portal_env: None) -> None:
    from hiveflow.spreadsheet.jobs import create_job, save_job

    job = create_job(filename="sample.xlsx", username="poc")
    job = save_job({**job, "status": "ready"})
    table = {
        "table_id": "t0",
        "entity_name": "customers",
        "purpose": "Customer master",
        "grain": "one row per customer",
        "confidence": 0.9,
        "status": "pending_review",
        "schema": [{"name": "customer_id", "type": "string", "description": "id", "is_key": True}],
        "profiling": {"columns": []},
        "source": {"sheet": "Customers", "row_count": 2},
        "chat_history": [{"role": "user", "text": "rename id column", "at": "now"}],
        "clean_shape_status": "rejected",
        "clean_goal": {
            "headers": ["customer_id"],
            "rows": [["C1"]],
            "row_count": 1,
            "preview_row_count": 1,
        },
    }
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse", "dbc"],
        active_source="sse",
        availability={"sse": True, "dbc": False},
        is_admin=True,
        job=job,
        report={"tables": [table]},
        active_tab="review",
    )
    assert "customers" in html
    assert 'id="spreadsheet-table-chat"' in html
    assert "spreadsheet-reject-compose" in html
    assert "rename id column" in html
    preview_start = html.find('id="spreadsheet-cleaned-preview"')
    chat_pos = html.find('id="spreadsheet-table-chat"')
    reject_box = html.find("spreadsheet-reject-box")
    assert preview_start != -1 and chat_pos != -1 and reject_box != -1
    assert reject_box < chat_pos
    reject_chunk = html[reject_box:chat_pos]
    assert "rename id column" not in reject_chunk
    assert "spreadsheet-reject-label" not in reject_chunk
    assert "spreadsheet-reject-submit" in reject_chunk
    assert chat_pos > html.find('id="spreadsheet-table-analysis"')
    assert "Approve table" in html
    assert 'role="tabpanel">' in html
    assert "spreadsheet-engine-panel-catalog" in html
    assert "Recent workbooks" not in html


def test_catalog_tab_lists_approved_proposals() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    catalog_entry = {
        "catalog_id": "job1__t0",
        "job_id": "job1",
        "table_id": "t0",
        "filename": "sample.xlsx",
        "entity_name": "customers",
        "approved_at": "2026-01-01T00:00:00+00:00",
        "approved_by": "poc",
        "last_upload_at": "2026-02-01T00:00:00+00:00",
        "transformation": {"version": 1, "steps": [{"op": "rename_columns", "mapping": {"a": "b"}}]},
        "proposal": {
            "table_id": "t0",
            "entity_name": "customers",
            "purpose": "Customer master",
            "grain": "one row per customer",
            "confidence": 0.9,
            "status": "approved",
            "schema": [{"name": "customer_id", "type": "string", "description": "id", "is_key": True}],
            "profiling": {"columns": []},
            "source": {"sheet": "Customers", "row_count": 2},
        },
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        catalog_entries=[catalog_entry],
        active_catalog=catalog_entry,
        active_tab="catalog",
    )
    assert "Approved catalog" in html
    assert "customers" in html
    assert "Last upload" in html
    assert "spreadsheet-catalog-detail" in html
    assert "Customer master" in html
    assert "Re-upload workbook" in html
    assert "linked_catalog_id" in html
    assert "spreadsheet-catalog-layout" in html
    assert "Output file" in html
    assert "spreadsheet-catalog-section" in html
    assert "spreadsheet-catalog-download" in html
    assert "/api/spreadsheet-engine/workbook?catalog_id=job1__t0" in html


def test_upload_form_renders_catalog_link_dropdown() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    catalog_entry = {
        "catalog_id": "sample__customers",
        "entity_name": "customers",
        "filename": "sample.xlsx",
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        catalog_entries=[catalog_entry],
        active_tab="analyze",
    )
    assert "linked_catalog_id" in html
    assert "sample__customers" in html


def test_transformation_panel_renders_in_proposal_review() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    report = {
        "table_count": 1,
        "tables": [
            {
                "table_id": "t0",
                "entity_name": "customers",
                "purpose": "Customer master",
                "grain": "one row per customer",
                "confidence": 0.9,
                "status": "pending_review",
                "pipeline_stage": "transform_review",
                "schema": [{"name": "customer_id", "type": "string"}],
                "profiling": {"columns": []},
                "clean_goal": {
                    "headers": ["customer_id"],
                    "rows": [["C1"]],
                    "row_count": 1,
                    "preview_row_count": 1,
                    "source": "oracle",
                },
                "clean_shape_status": "approved",
                "transformation": {
                    "version": 1,
                    "steps": [{"op": "rename_columns", "mapping": {"Customer ID": "customer_id"}}],
                },
                "transformation_status": "pending_review",
                "transformation_confidence": 0.4,
                "transformation_notes": [
                    "Synthesizing steps to match approved clean goal (1 goal row(s))",
                    "AI proposed 2 step(s)",
                ],
            }
        ],
    }
    transform_preview = {
        "transformation_preview": {
            "before": {"headers": ["Customer ID"], "rows": [["C1"]], "row_count": 1, "preview_row_count": 1},
            "after": {"headers": ["customer_id"], "rows": [["C1"]], "row_count": 1, "preview_row_count": 1},
        }
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job1", "status": "ready", "filename": "sample.xlsx"},
        report=report,
        request_job_id="job1",
        active_tab="review",
        transform_preview=transform_preview,
    )
    assert "spreadsheet-transform-panel" in html
    assert "Approve transform output" in html
    assert ">Reject<" in html or ">Reject</button>" in html
    assert "Approved AI cleaned (goal)" in html
    assert "Deterministic transform output" in html
    assert "spreadsheet-stage-stepper" in html
    assert "Transform review" in html
    assert "spreadsheet-transform-head-meta" in html
    assert "via oracle" in html
    assert "Confidence 40%" in html
    assert "spreadsheet-step-notes" in html
    assert "spreadsheet-transform-action-btns" in html


def test_join_review_renders_dna_proposals() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    report = {
        "table_count": 1,
        "tables": [
            {
                "table_id": "t0",
                "entity_name": "price_list",
                "purpose": "Item prices",
                "grain": "one row per item",
                "status": "approved",
                "join_status": "pending_review",
                "join_proposals": [
                    {
                        "id": "silver:dbc:items:item_no:id",
                        "layer": "silver",
                        "target": "items",
                        "left_key": "item_no",
                        "right_key": "id",
                        "confidence": 0.9,
                        "match_reason": "grain overlap (item); key stem of item_no matches items.id",
                        "selected": True,
                    },
                    {
                        "id": "gold:dna:fact_item_margin:item_no:itemid",
                        "layer": "gold",
                        "target": "fact_item_margin",
                        "left_key": "item_no",
                        "right_key": "itemId",
                        "confidence": 0.75,
                        "match_reason": "gold grain columns include this key",
                        "selected": True,
                    },
                ],
                "schema": [{"name": "item_no", "type": "string", "is_key": True}],
                "profiling": {"columns": []},
                "clean_goal": {"headers": ["item_no"], "rows": [["A1"]], "row_count": 1},
                "clean_shape_status": "approved",
                "transformation": {"steps": [{"op": "cast"}]},
                "transformation_status": "approved",
            }
        ],
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job1", "status": "ready", "filename": "prices.xlsx"},
        report=report,
        request_job_id="job1",
        active_tab="review",
    )
    assert "spreadsheet-join-panel" in html
    assert "Propose lake joins" in html
    assert "fact_item_margin" in html
    assert "Approve selected joins" in html
    assert "Re-run DNA joins" in html


def test_reload_validation_passed_renders_complete_button() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    report = {
        "table_count": 1,
        "tables": [
            {
                "table_id": "t0",
                "entity_name": "customers",
                "purpose": "Customers",
                "reload_mode": True,
                "reload_validation_status": "passed",
                "linked_catalog_id": "sample__customers",
                "transformation": {"version": 1, "steps": []},
                "transformation_status": "approved",
                "schema": [{"name": "customer_id", "type": "string"}],
            }
        ],
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job1", "status": "ready", "filename": "sample.xlsx", "reupload": True},
        report=report,
        request_job_id="job1",
        active_tab="review",
    )
    assert "Reload validation passed" in html
    assert "Complete reload" in html
    assert "No AI analysis was run" in html


def test_reload_validation_failed_renders_recovery_options() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    report = {
        "table_count": 1,
        "tables": [
            {
                "table_id": "t0",
                "entity_name": "customers",
                "reload_mode": True,
                "reload_validation_status": "failed",
                "reload_validation_issues": ["Expected column 'company' missing from transformed output"],
                "linked_catalog_id": "sample__customers",
                "transformation": {"version": 1, "steps": []},
                "schema": [{"name": "customer_id", "type": "string"}],
            }
        ],
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job1", "status": "ready", "filename": "sample.xlsx", "reupload": True},
        report=report,
        request_job_id="job1",
        active_tab="review",
    )
    assert "Reload validation failed" in html
    assert "Upload a different file" in html
    assert "Rewrite schema with AI" in html
    assert "Propose new transformation with AI" in html


def test_spreadsheet_pipeline_progress_includes_propose_stage() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.service import spreadsheet_pipeline_progress

    pipeline = spreadsheet_pipeline_progress("proposing")
    labels = [stage["label"] for stage in pipeline["stages"]]
    assert "Propose transformations" in labels
    assert pipeline["stages"][3]["state"] == "active"
    assert pipeline["status_label"] == "Generating cleaned proposals"
    assert "cleaned proposal of all tables" in pipeline["status_detail"]


def test_in_progress_reload_job_uses_approved_steps_copy() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    job = {
        "job_id": "job-reload",
        "status": "running",
        "filename": "sample.xlsx",
        "linked_catalog_id": "sample__customers",
        "reupload": True,
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job=job,
        request_job_id="job-reload",
        active_tab="review",
        status_url="/api/spreadsheet-engine/status",
    )
    assert "Approved steps for this workbook are being applied for final approval" in html
    assert "spreadsheet-proposal-stages" not in html


def test_review_tab_shows_sheet_checkboxes_before_proposals() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    job = {
        "job_id": "job-sheets",
        "status": "awaiting_sheets",
        "filename": "multi.xlsx",
        "sheet_names": ["Customers", "Notes"],
        "sheets": [
            {"name": "Customers", "table_count": 1},
            {"name": "Notes", "table_count": 1},
        ],
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job=job,
        request_job_id="job-sheets",
        active_tab="review",
    )
    assert 'id="spreadsheet-sheet-select"' in html
    assert 'name="sheet" value="Customers"' in html
    assert 'name="sheet" value="Notes"' in html
    assert 'type="checkbox"' in html
    assert "Generate proposals" in html
    assert 'id="spreadsheet-proposal-status"' not in html
    assert 'id="spreadsheet-table-analysis"' not in html


def test_proposal_review_hides_chat_until_rejected() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    table = {
        "table_id": "t0",
        "entity_name": "customers",
        "purpose": "Customer master",
        "status": "pending_review",
        "pipeline_stage": "clean_review",
        "clean_shape_status": "pending_review",
        "clean_goal": {
            "headers": ["customer_id"],
            "rows": [["C1"]],
            "row_count": 1,
            "preview_row_count": 1,
        },
        "schema": [{"name": "customer_id", "type": "string"}],
        "profiling": {"columns": []},
        "source": {"sheet": "Customers", "row_count": 1},
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job1", "status": "ready", "filename": "sample.xlsx"},
        report={"tables": [table]},
        active_tab="review",
        table_preview={
            "headers": ["customer_id"],
            "rows": [["C1"], ["C2"], ["C3"]],
            "row_count": 3,
            "preview_row_count": 3,
        },
    )
    assert "Approve cleaned data" in html
    assert "Reject" in html
    assert "spreadsheet-reject-box" in html
    assert "spreadsheet-reject-submit" in html
    assert "spreadsheet-reject-label" not in html
    assert "spreadsheet-table-head-reject" in html
    head_pos = html.find("spreadsheet-table-head-reject")
    source_pos = html.find("Source data preview")
    assert head_pos != -1 and source_pos != -1
    assert head_pos < source_pos
    assert 'name="action" value="reject_table"' in html
    head_form = html[head_pos : html.find("</form>", head_pos)]
    assert "<textarea" not in head_form
    assert "Reject" in head_form
    assert 'id="spreadsheet-table-chat"' not in html
    assert "Cleaned preview" in html
    assert 'id="spreadsheet-cleaned-preview"' in html
    source_pos = html.find("Source data preview")
    cleaned_pos = html.find("Cleaned preview")
    assert source_pos != -1 and cleaned_pos != -1
    assert source_pos < cleaned_pos


def test_discarded_tables_are_hidden_from_proposals() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    keep = {
        "table_id": "t0",
        "entity_name": "customers",
        "purpose": "Keep me",
        "status": "pending_review",
        "pipeline_stage": "clean_review",
        "clean_shape_status": "pending_review",
        "clean_goal": {"headers": ["customer_id"], "rows": [["C1"]], "row_count": 1},
        "schema": [{"name": "customer_id", "type": "string"}],
        "profiling": {"columns": []},
        "source": {"sheet": "Customers", "row_count": 1},
    }
    gone = {
        **keep,
        "table_id": "t1",
        "entity_name": "noise_table",
        "purpose": "Drop me",
        "status": "discarded",
    }
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job1", "status": "ready", "filename": "sample.xlsx"},
        report={"tables": [keep, gone]},
        active_tab="review",
    )
    assert "customers" in html
    assert "noise_table" not in html
    assert "1 proposed table" in html


def test_proposals_keep_prior_uploads_and_navigate_files() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    table_a = {
        "table_id": "t0",
        "entity_name": "customers",
        "purpose": "Keep me",
        "status": "pending_review",
        "pipeline_stage": "clean_review",
        "clean_shape_status": "pending_review",
        "clean_goal": {"headers": ["customer_id"], "rows": [["C1"]], "row_count": 1},
        "schema": [{"name": "customer_id", "type": "string"}],
        "profiling": {"columns": []},
        "source": {"sheet": "Customers", "row_count": 1},
    }
    table_b = {**table_a, "table_id": "t0", "entity_name": "vendors", "purpose": "Vendor master"}
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job={"job_id": "job-new", "status": "ready", "filename": "vendors.xlsx"},
        report={"tables": [table_b]},
        active_tab="review",
        proposal_jobs=[
            {"job_id": "job-new", "status": "ready", "filename": "vendors.xlsx"},
            {"job_id": "job-old", "status": "ready", "filename": "customers.xlsx"},
        ],
    )
    assert "spreadsheet-file-nav" in html
    assert "vendors.xlsx" in html
    assert "customers.xlsx" in html
    assert "job_id=job-old" in html
    assert "job_id=job-new" in html
    assert "Previous file" in html or "Next file" in html
    assert 'name="action" value="reject_job"' in html
    assert "Reject file" in html
    assert "vendors" in html
    assert "aria-label=\"Proposed tables\"" in html


def test_file_pager_has_per_chip_reject_and_reject_all_for_admin() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    proposal_jobs = [
        {"job_id": f"job-{i}", "status": "ready", "filename": "sample.xlsx"}
        for i in range(3)
    ]
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job=proposal_jobs[0],
        report={"tables": []},
        active_tab="review",
        proposal_jobs=proposal_jobs,
    )
    # A per-chip reject form for every workbook, not just the selected one.
    assert html.count('class="spreadsheet-file-chip-reject"') == 3
    for job in proposal_jobs:
        assert f'name="job_id" value="{job["job_id"]}"' in html
    # One bulk action.
    assert 'name="action" value="reject_all_jobs"' in html
    assert "Reject all files" in html


def test_file_pager_hides_reject_controls_for_non_admin() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    proposal_jobs = [
        {"job_id": "job-0", "status": "ready", "filename": "sample.xlsx"},
        {"job_id": "job-1", "status": "ready", "filename": "sample.xlsx"},
    ]
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=False,
        job=proposal_jobs[0],
        report={"tables": []},
        active_tab="review",
        proposal_jobs=proposal_jobs,
    )
    assert "spreadsheet-file-chip-reject" not in html
    assert "reject_all_jobs" not in html


def test_in_progress_job_renders_on_review_tab() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page

    job = {"job_id": "job-abc", "status": "running", "filename": "sample.xlsx"}
    html = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job=job,
        request_job_id="job-abc",
        active_tab="review",
        status_url="/api/spreadsheet-engine/status",
    )
    assert 'data-spreadsheet-panel="review"' in html
    assert "spreadsheet-proposal-status" in html
    assert "AI is generating a cleaned proposal of all tables" in html
    assert "spreadsheet-proposal-stages" not in html
    assert "Parse workbook" not in html
    assert "api/spreadsheet-engine/status" in html
    assert 'id="spreadsheet-engine-panel-review"' in html
    assert 'role="tabpanel">' in html


def test_spreadsheet_pipeline_progress_maps_job_status() -> None:
    from hiveflow.dna.web.portal.spreadsheet_engine.service import spreadsheet_pipeline_progress

    pipeline = spreadsheet_pipeline_progress(
        "profiling",
        execution_status="running",
    )
    assert pipeline["status_label"] == "Generating cleaned proposals"
    assert pipeline["execution_status"] == "running"
    assert pipeline["stages"][0]["state"] == "complete"
    assert pipeline["stages"][1]["state"] == "active"


def test_job_status_includes_pipeline_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HIVEFLOW_S3_BUCKET", raising=False)

    from hiveflow.dna.settings import DnaSettings
    from hiveflow.dna.web.portal.spreadsheet_engine.service import job_status
    from hiveflow.spreadsheet.jobs import create_job, save_job

    job = create_job(filename="sample.xlsx", username="poc")
    job = save_job(
        {
            **job,
            "status": "interpreting",
            "execution_arn": "arn:aws:states:us-east-2:123:execution:spreadsheet:abc",
        }
    )

    class _Sf:
        def describe_execution(self, *, executionArn: str):
            assert executionArn.endswith(":abc")
            return {"status": "RUNNING"}

    class _Boto3:
        def client(self, name: str, **kwargs):
            if name == "stepfunctions":
                return _Sf()
            raise AssertionError(f"unexpected boto3 client {name!r}")

    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto3())

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    payload = job_status(settings, job_id=job["job_id"], company="poc", environment="dev")
    assert payload["execution_status"] == "running"
    assert payload["pipeline"]["status_label"] == "Generating cleaned proposals"
    assert payload["pipeline"]["stages"][2]["state"] == "active"


def test_job_status_reports_per_table_progress_while_proposing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the Map state's branches are still running, job_status() should
    expose per-table ready/pending progress instead of one job-wide spinner."""
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HIVEFLOW_S3_BUCKET", raising=False)

    from hiveflow.dna.settings import DnaSettings
    from hiveflow.dna.web.portal.spreadsheet_engine.service import job_status
    from hiveflow.spreadsheet.jobs import _write_json, create_job, save_job
    from hiveflow.storage.paths import spreadsheet_engine_job_table_key

    job = create_job(filename="sample.xlsx", username="poc")
    job_id = job["job_id"]
    save_job({**job, "status": "proposing", "table_ids": ["t0", "t1", "t2"]})

    # t0: interpreted but not yet proposed (no clean_goal) — still pending.
    _write_json(spreadsheet_engine_job_table_key(job_id, "t0"), {"table_id": "t0", "entity_name": "customers"})
    # t1: proposed — ready.
    _write_json(
        spreadsheet_engine_job_table_key(job_id, "t1"),
        {"table_id": "t1", "entity_name": "vendors", "clean_goal": {"headers": ["a"], "rows": []}},
    )
    # t2: never written by interpret/propose — still pending.

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    payload = job_status(settings, job_id=job_id, company="poc", environment="dev")

    progress = {item["table_id"]: item for item in payload["table_progress"]}
    assert progress["t0"]["status"] == "pending"
    assert progress["t1"]["status"] == "ready"
    assert progress["t1"]["entity_name"] == "vendors"
    assert progress["t2"]["status"] == "pending"


def test_upload_redirects_to_review_tab(tmp_path: Path, portal_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from io import BytesIO

    from openpyxl import Workbook

    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(["Customer ID", "Company"])
    ws.append(["C1", "Acme"])
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "")

    response = client.post(
        "/portal/semantics/source-docs/sse",
        data={
            "action": "upload",
            "workbook": (
                buffer,
                "sample.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    if response.status_code != 302:
        html = response.get_data(as_text=True)
        assert response.status_code == 302, html[:2000]
    location = response.headers.get("Location") or ""
    assert "job_id=" in location
    assert "tab=review" in location

    follow = client.get(location)
    assert follow.status_code == 200
    html = follow.get_data(as_text=True)
    assert "spreadsheet-sheet-select" in html
    assert 'name="sheet"' in html
    assert "Customers" in html
    assert 'id="spreadsheet-proposal-status"' not in html
    assert 'id="spreadsheet-table-analysis"' not in html


def test_review_tab_portal_footer_stays_inside_main_column() -> None:
    """Regression: malformed review tabpanel HTML used to eject the portal footer."""
    from bs4 import BeautifulSoup

    from hiveflow.dna.web.portal.spreadsheet_engine.render import render_spreadsheet_engine_page
    from hiveflow.dna.web.theme import render_portal_page

    job = {
        "job_id": "job-1",
        "status": "ready",
        "filename": "sample.xlsx",
    }
    table = {
        "table_id": "t0",
        "entity_name": "customers",
        "purpose": "Customer master",
        "grain": "one row per customer",
        "confidence": 0.9,
        "status": "pending_review",
        "schema": [{"name": "customer_id", "type": "string", "description": "id", "is_key": True}],
        "profiling": {"columns": []},
        "source": {"sheet": "Customers", "row_count": 2},
    }
    body = render_spreadsheet_engine_page(
        url=lambda path: path,
        sources=["sse"],
        active_source="sse",
        availability={"sse": True},
        is_admin=True,
        job=job,
        report={"tables": [table]},
        active_tab="review",
    )

    class _Client:
        display_name = "POC"

    page = render_portal_page(
        title="Spreadsheet Engine",
        active_path="/portal/semantics/source-docs/sse",
        body=body,
        nav_links=(),
        client=_Client(),
        url=lambda p: p,
        side_nav_title="DNA",
        side_nav_items=(("Spreadsheet Engine", "/portal/semantics/source-docs/sse"),),
        side_nav_id="dna-nav",
    )
    soup = BeautifulSoup(page, "html.parser")
    footer = soup.select_one("footer.portal-footer")
    portal_main = soup.select_one(".portal-main")
    assert footer is not None
    assert portal_main is not None
    assert footer.parent == portal_main
