"""Tests for the Spreadsheet Lab phase-1 (extraction) backend.

Mirrors the conventions in ``test_spreadsheet_engine.py``: ``HIVEFLOW_DATA_DIR``
points storage at ``tmp_path`` (no ``HIVEFLOW_S3_BUCKET`` set, so
``blobstore.resolve_blob_location`` resolves to local-disk mode), and every
agent call passes ``invoke=False`` so nothing ever reaches Bedrock.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook


def _build_grouped_price_workbook(path: Path) -> None:
    """Mirrors test_spreadsheet_engine.py's fixture of the same shape — a
    messy price-list export with a report preamble and grouped continuation
    rows, specifically so the deterministic ``_heuristic_group_rows_spec``
    path (key column ``no`` + a ``unit_price`` coalesce target) fires under
    ``invoke=False`` without needing Bedrock."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["List Price Sheet as of 07/01/27"])
    ws.append(["Phone: +1 425 555 0100", None, None, None, None, None, None, None, None, None, None, None, None, "Page", 1])
    ws.append([None] * 7)
    ws.append(["CRONUS International Ltd."])
    ws.append([None] * 7)
    ws.append(["All Customers"])
    ws.append([None] * 7)
    ws.append([None] * 7)
    ws.append(
        [
            "No.",
            "Description",
            "Variant Code",
            "Minimum Quantity",
            None,
            "Unit of Measure Code",
            "Unit Price",
            "Starting Date",
            None,
            "Ending Date",
        ]
    )
    ws.append(["1896-S", "ATHENS Desk", None, "", None, None, None, None, None, None])
    ws.append(["", None, None, None, None, "PCS", 1000.8, "", None, None])
    ws.append(["1900-S", "PARIS Guest Chair, black", None, "", None, None, None, None, None, None])
    ws.append(["", None, None, None, None, "PCS", 192.8, "", None, None])
    wb.save(path)


def _write_sample_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(["customer_id", "name", "region"])
    ws.append([1, "Acme", "West"])
    ws.append([2, "Globex", "East"])
    wb.save(path)


def test_run_parse_creates_pending_table_with_heuristic_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import intake, store

    job = intake.create_job(filename="sample.xlsx", username="poc")
    workbook_path = tmp_path / "upload.xlsx"
    _write_sample_workbook(workbook_path)
    intake.store_upload(job["job_id"], filename="sample.xlsx", body=workbook_path.read_bytes())

    job = intake.run_parse(job["job_id"], invoke=False)
    assert job["status"] == "awaiting_extract_review"
    assert job["table_ids"] == ["t0"]
    assert job["auto_replayed"] is False

    table = store.load_table(job["job_id"], "t0")
    assert table is not None
    assert table["phase"] == "extract"
    assert table["status"] == "pending_review"
    assert table["extract_proposal"] is not None
    assert table["extract_proposal"]["entity_name"] == "customers"
    assert table["input_shape"]["shape_hash"]


def test_approve_all_tables_compiles_file_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import extract_review, file_recipe, intake, store

    job = intake.create_job(filename="sample.xlsx", username="poc")
    workbook_path = tmp_path / "upload.xlsx"
    _write_sample_workbook(workbook_path)
    intake.store_upload(job["job_id"], filename="sample.xlsx", body=workbook_path.read_bytes())
    job = intake.run_parse(job["job_id"], invoke=False)

    extract_review.approve_extraction(job["job_id"], "t0", invoke=False)

    job = store.load_job(job["job_id"])
    assert job is not None
    assert job["status"] == "awaiting_clean_review"

    recipe = file_recipe.find_matching_file_recipe(job["file_shape_hash"])
    assert recipe is not None
    assert recipe["tables"][0]["table_id"] == "t0"
    assert recipe["tables"][0]["status"] == "approved"


def test_reject_extraction_tracks_feedback_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import extract_review, intake, store

    job = intake.create_job(filename="sample.xlsx", username="poc")
    workbook_path = tmp_path / "upload.xlsx"
    _write_sample_workbook(workbook_path)
    intake.store_upload(job["job_id"], filename="sample.xlsx", body=workbook_path.read_bytes())
    job = intake.run_parse(job["job_id"], invoke=False)

    updated = extract_review.reject_extraction(
        job["job_id"],
        "t0",
        feedback="Header is actually one row lower",
        by="jeremiahdavidstephens@gmail.com",
        invoke=False,
    )
    assert updated["attempt_count"] == 1
    assert len(updated["feedback_history"]) == 1
    assert updated["feedback_history"][0]["text"] == "Header is actually one row lower"

    # invoke=False threads through the retry too, so run_table_task's heuristic
    # path re-ran synchronously (no AWS_LAMBDA_FUNCTION_NAME in tests) and the
    # table is back to pending_review with a fresh proposal.
    table = store.load_table(job["job_id"], "t0")
    assert table is not None
    assert table["status"] == "pending_review"
    assert table["extract_proposal"] is not None


def test_reject_extraction_requires_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import extract_review, intake

    job = intake.create_job(filename="sample.xlsx", username="poc")
    workbook_path = tmp_path / "upload.xlsx"
    _write_sample_workbook(workbook_path)
    intake.store_upload(job["job_id"], filename="sample.xlsx", body=workbook_path.read_bytes())
    job = intake.run_parse(job["job_id"], invoke=False)

    with pytest.raises(ValueError):
        extract_review.reject_extraction(job["job_id"], "t0", feedback="   ", invoke=False)


def test_reupload_matching_file_auto_replays_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import extract_review, intake, store

    workbook_path = tmp_path / "upload.xlsx"
    _write_sample_workbook(workbook_path)
    body = workbook_path.read_bytes()

    first_job = intake.create_job(filename="sample.xlsx", username="poc")
    intake.store_upload(first_job["job_id"], filename="sample.xlsx", body=body)
    first_job = intake.run_parse(first_job["job_id"], invoke=False)
    extract_review.approve_extraction(first_job["job_id"], "t0", invoke=False)

    second_job = intake.create_job(filename="sample.xlsx", username="poc")
    intake.store_upload(second_job["job_id"], filename="sample.xlsx", body=body)
    second_job = intake.run_parse(second_job["job_id"], invoke=False)

    assert second_job["auto_replayed"] is True
    assert second_job["matched_file_recipe_id"] == second_job["file_shape_hash"]

    # A saved recipe skips the AI call, not the operator's review — the table
    # comes back pending_review with its prior proposal pre-filled, awaiting
    # a (zero-AI-call) confirm click, same as any other upload.
    assert second_job["status"] == "awaiting_extract_review"
    table = store.load_table(second_job["job_id"], "t0")
    assert table is not None
    assert table["phase"] == "extract"
    assert table["status"] == "pending_review"
    assert table["extract_proposal"] is not None

    # Confirming it carries the table into phase 2 exactly like a fresh
    # upload's approval would (no matching table recipe yet for this shape).
    extract_review.approve_extraction(second_job["job_id"], "t0", invoke=False)
    second_job = store.load_job(second_job["job_id"])
    assert second_job is not None
    assert second_job["status"] == "awaiting_clean_review"
    table = store.load_table(second_job["job_id"], "t0")
    assert table is not None
    assert table["phase"] == "clean"
    assert table["status"] == "pending_shape_review"


def test_force_rerun_ignores_matched_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import extract_review, intake

    workbook_path = tmp_path / "upload.xlsx"
    _write_sample_workbook(workbook_path)
    body = workbook_path.read_bytes()

    first_job = intake.create_job(filename="sample.xlsx", username="poc")
    intake.store_upload(first_job["job_id"], filename="sample.xlsx", body=body)
    first_job = intake.run_parse(first_job["job_id"], invoke=False)
    extract_review.approve_extraction(first_job["job_id"], "t0", invoke=False)

    second_job = intake.create_job(filename="sample.xlsx", username="poc")
    intake.store_upload(second_job["job_id"], filename="sample.xlsx", body=body)
    second_job = intake.force_rerun(second_job["job_id"], invoke=False)

    assert second_job["status"] == "awaiting_extract_review"
    assert second_job["auto_replayed"] is False


def test_full_pipeline_extract_clean_materialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: approve extraction -> phase 2 auto-starts -> approve clean
    shape -> approve transformation -> materializes to silver/reference_lab/."""
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import clean_review, extract_review, intake, store

    workbook_path = tmp_path / "price_list.xlsx"
    _build_grouped_price_workbook(workbook_path)

    job = intake.create_job(filename="price_list.xlsx", username="poc")
    intake.store_upload(job["job_id"], filename="price_list.xlsx", body=workbook_path.read_bytes())
    job = intake.run_parse(job["job_id"], invoke=False)
    assert job["table_ids"] == ["t0"]

    # Approving extraction should auto-kick phase 2 (start_clean_phase), which
    # runs synchronously in tests (no AWS_LAMBDA_FUNCTION_NAME).
    extract_review.approve_extraction(job["job_id"], "t0", invoke=False)

    table = store.load_table(job["job_id"], "t0")
    assert table is not None
    assert table["entity_name"]  # promoted from extract_proposal
    assert table["phase"] == "clean"
    assert table["status"] == "pending_shape_review"
    assert table["clean_goal"] is not None
    assert len(table["clean_goal"]["rows"]) == 2  # grouped down from 4 raw rows

    clean_review.approve_clean_shape(job["job_id"], "t0", invoke=False)
    table = store.load_table(job["job_id"], "t0")
    assert table["clean_shape_status"] == "approved"
    assert table["status"] == "pending_transform_review"
    steps = table["transformation"]["steps"]
    assert any(step.get("op") == "group_rows" for step in steps)

    finished = clean_review.approve_transformation(job["job_id"], "t0")
    assert finished["transformation_status"] == "approved"
    assert finished["phase"] == "done"
    assert finished["silver"] is not None
    assert finished["silver"]["silver_source"] == "reference_lab"
    assert finished["silver"]["silver_row_count"] == 2

    job = store.load_job(job["job_id"])
    assert job is not None
    assert job["status"] == "ready"

    parquet_path = tmp_path / "silver" / "reference_lab" / finished["silver"]["silver_entity"] / "data.parquet"
    assert parquet_path.exists()

    # Table-level recipe was saved and keyed by this table's own input shape.
    from hiveflow.spreadsheet_lab import table_recipe

    recipe = table_recipe.find_matching_table_recipe(table["input_shape"])
    assert recipe is not None
    assert any(step.get("op") == "group_rows" for step in recipe["transformation"]["steps"])


def test_reupload_replays_both_file_and_table_recipes_with_no_ai_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import clean_review, extract_review, intake, store

    workbook_path = tmp_path / "price_list.xlsx"
    _build_grouped_price_workbook(workbook_path)
    body = workbook_path.read_bytes()

    first_job = intake.create_job(filename="price_list.xlsx", username="poc")
    intake.store_upload(first_job["job_id"], filename="price_list.xlsx", body=body)
    first_job = intake.run_parse(first_job["job_id"], invoke=False)
    extract_review.approve_extraction(first_job["job_id"], "t0", invoke=False)
    clean_review.approve_clean_shape(first_job["job_id"], "t0", invoke=False)
    clean_review.approve_transformation(first_job["job_id"], "t0")

    # Re-upload the identical workbook. invoke is intentionally omitted on
    # run_parse (defaults to None / "really call the agent") to prove the
    # replay path never reaches it.
    second_job = intake.create_job(filename="price_list.xlsx", username="poc")
    intake.store_upload(second_job["job_id"], filename="price_list.xlsx", body=body)
    second_job = intake.run_parse(second_job["job_id"])

    assert second_job["auto_replayed"] is True
    assert second_job["status"] == "awaiting_extract_review"

    table = store.load_table(second_job["job_id"], "t0")
    assert table is not None
    assert table["phase"] == "extract"
    assert table["status"] == "pending_review"

    # Confirming extraction (invoke=False here just documents "still no AI
    # call" — approve_extraction never calls the agent regardless) carries
    # the table into phase 2 via the matching table recipe, landing it at
    # the transform-review checkpoint with zero AI calls too.
    extract_review.approve_extraction(second_job["job_id"], "t0", invoke=False)
    table = store.load_table(second_job["job_id"], "t0")
    assert table is not None
    assert table["phase"] == "clean"
    assert table["status"] == "pending_transform_review"
    assert table["clean_shape_status"] == "approved"

    finished = clean_review.approve_transformation(second_job["job_id"], "t0")
    assert finished["phase"] == "done"
    assert finished["silver"] is not None

    second_job = store.load_job(second_job["job_id"])
    assert second_job is not None
    assert second_job["status"] == "ready"


def test_phase2_discard_is_remembered_on_reupload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table discarded during phase 2 (clean_review.discard_table) should
    replay straight to discarded on a later re-upload, not go through review
    all over again — the file recipe is only compiled once at phase 1
    (before phase 2 discards can happen), so this only works if the job's
    finish also recompiles it (see clean_review._maybe_finish_job)."""
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))

    from hiveflow.spreadsheet_lab import clean_review, extract_review, intake, store

    workbook_path = tmp_path / "price_list.xlsx"
    _build_grouped_price_workbook(workbook_path)
    body = workbook_path.read_bytes()

    first_job = intake.create_job(filename="price_list.xlsx", username="poc")
    intake.store_upload(first_job["job_id"], filename="price_list.xlsx", body=body)
    first_job = intake.run_parse(first_job["job_id"], invoke=False)
    extract_review.approve_extraction(first_job["job_id"], "t0", invoke=False)
    clean_review.discard_table(first_job["job_id"], "t0")

    first_job = store.load_job(first_job["job_id"])
    assert first_job is not None
    assert first_job["status"] == "ready"
    table = store.load_table(first_job["job_id"], "t0")
    assert table is not None
    assert table["status"] == "discarded"

    second_job = intake.create_job(filename="price_list.xlsx", username="poc")
    intake.store_upload(second_job["job_id"], filename="price_list.xlsx", body=body)
    second_job = intake.run_parse(second_job["job_id"])

    assert second_job["auto_replayed"] is True
    table = store.load_table(second_job["job_id"], "t0")
    assert table is not None
    assert table["status"] == "discarded"
    # Every table is terminal (discarded) with none pending review, so the
    # job should already be finished with no human action required.
    second_job = store.load_job(second_job["job_id"])
    assert second_job is not None
    assert second_job["status"] == "ready"
