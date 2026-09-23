"""DNA Engine route registration — one function per feature area.

Each handler reads the session/tenant context the app's auth middleware
already bound to ``request.state`` (see ``app.py::_session_auth``), then
calls straight into the shell's existing, framework-agnostic
``hiveflow.dna.web.portal.views``/``portal.semantics`` render functions —
reused unchanged via the ``chrome.REQUEST`` shim (see ``chrome.py``'s module
docstring for why that's safe) rather than duplicated. Route paths drop the
shell's old ``/portal`` prefix — this is its own app on its own host now, the
same reasoning ``hiveflow.spreadsheet_lab.web.app`` used for its own routes.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from hiveflow.dna_engine.web.chrome import REQUEST, to_starlette_response


def _ctx(request: Request):
    """(settings, client, is_admin) bound by the auth middleware for this request."""
    return request.state.dna_settings, request.state.client, request.state.is_admin


def _configured_reference_sources(portal_settings) -> list[str]:
    from hiveflow.project_config import get_environment_config, iter_configured_connectors

    from hiveflow.dna_engine.web.tenant import hosting_environment

    try:
        env_cfg = get_environment_config(portal_settings.company, hosting_environment())
    except Exception:  # noqa: BLE001
        return [portal_settings.source]
    return [name for name, _cfg in iter_configured_connectors(env_cfg)]


def register_catalog_routes(app: FastAPI) -> None:
    from hiveflow.dna.web.portal.views import (
        render_catalog,
        render_catalog_gold,
        render_catalog_silver,
        render_catalog_table,
        render_dna,
    )

    @app.get("/dna")
    async def dna_landing(request: Request):
        settings, client, is_admin = _ctx(request)
        response = render_dna(REQUEST, settings=settings, client=client, is_admin=is_admin)
        return to_starlette_response(response)

    @app.get("/catalog")
    async def catalog_index(request: Request):
        settings, client, is_admin = _ctx(request)
        response = render_catalog(REQUEST, settings=settings, client=client, is_admin=is_admin)
        return to_starlette_response(response)

    @app.get("/catalog/gold")
    async def catalog_gold(request: Request):
        settings, client, is_admin = _ctx(request)
        response = render_catalog_gold(REQUEST, settings=settings, client=client, is_admin=is_admin)
        return to_starlette_response(response)

    @app.get("/catalog/silver")
    async def catalog_silver(request: Request):
        settings, client, is_admin = _ctx(request)
        response = render_catalog_silver(REQUEST, settings=settings, client=client, is_admin=is_admin)
        return to_starlette_response(response)

    @app.get("/catalog/silver/{entity}")
    async def catalog_silver_entity(request: Request, entity: str):
        settings, client, is_admin = _ctx(request)
        response = render_catalog_silver(
            REQUEST, settings=settings, client=client, entity=entity, is_admin=is_admin
        )
        return to_starlette_response(response)

    @app.get("/catalog/{output_id}")
    async def catalog_output(request: Request, output_id: str):
        settings, client, is_admin = _ctx(request)
        response = render_catalog_table(
            REQUEST, settings=settings, client=client, output_id=output_id, is_admin=is_admin
        )
        return to_starlette_response(response)


def register_data_profile_routes(app: FastAPI) -> None:
    from hiveflow.dna.web.portal.views import render_data_profile_detail, render_data_profile_index

    @app.get("/dna/data-profile")
    async def data_profile_index(request: Request):
        settings, client, is_admin = _ctx(request)
        response = render_data_profile_index(
            REQUEST,
            settings=settings,
            client=client,
            is_admin=is_admin,
            configured_sources=_configured_reference_sources(settings),
        )
        return to_starlette_response(response)

    @app.post("/dna/data-profile/{source}/refresh")
    async def data_profile_source_refresh(request: Request, source: str):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return PlainTextResponse("Forbidden", status_code=403)
        from hiveflow.dna.web.portal.data_profile_ui.service import refresh_source

        try:
            result = refresh_source(settings, source)
            params = {"msg": f"Re-profiled {result.get('profiled_count', 0)} table(s) in {source}."}
        except Exception as exc:  # noqa: BLE001
            params = {"err": str(exc)}
        return RedirectResponse(f"/dna/data-profile?{urlencode(params)}", status_code=302)

    @app.api_route("/dna/data-profile/{source}/{entity}", methods=["GET", "POST"])
    async def data_profile_entity(request: Request, source: str, entity: str):
        settings, client, is_admin = _ctx(request)

        if request.method == "POST":
            if not is_admin:
                return PlainTextResponse("Forbidden", status_code=403)
            from hiveflow.dna.web.portal.data_profile_ui.service import refresh_entity, save_overrides

            form = await request.form()
            action = str(form.get("action") or "").strip()
            try:
                if action == "refresh_entity":
                    refresh_entity(settings, source, entity)
                    params = {"msg": "Re-ran Bedrock for this table."}
                elif action == "save_overrides":
                    purpose = form.get("purpose")
                    field_descriptions = {
                        key[len("description__") :]: value
                        for key, value in form.items()
                        if key.startswith("description__")
                    }
                    save_overrides(
                        settings,
                        source,
                        entity,
                        purpose=str(purpose) if purpose is not None else None,
                        field_descriptions=field_descriptions,
                    )
                    params = {"msg": "Saved manual overrides."}
                else:
                    params = {"err": f"Unknown action {action!r}"}
            except ValueError as exc:
                params = {"err": str(exc)}
            return RedirectResponse(
                f"/dna/data-profile/{source}/{entity}?{urlencode(params)}", status_code=302
            )

        response = render_data_profile_detail(
            REQUEST,
            settings=settings,
            client=client,
            source=source,
            entity=entity,
            is_admin=is_admin,
            message=str(request.query_params.get("msg") or ""),
            error=str(request.query_params.get("err") or ""),
        )
        return to_starlette_response(response)


def register_model_mapping_routes(app: FastAPI) -> None:
    from hiveflow.dna.web.portal.views import render_model_mapping

    @app.api_route("/dna/model-mapping", methods=["GET", "POST"])
    async def model_mapping(request: Request):
        settings, client, is_admin = _ctx(request)
        entity_param = str(request.query_params.get("entity") or "").strip()

        if request.method == "POST":
            if not is_admin:
                return PlainTextResponse("Forbidden", status_code=403)
            from hiveflow.dna.web.portal.model_mapping import service as mapping_service

            form = await request.form()
            action = str(form.get("action") or "").strip()
            entity_id = str(form.get("entity_id") or "").strip()
            params: dict[str, str] = {}
            try:
                if action == "init":
                    mapping_service.initialize(settings, str(form.get("industry_pack_id") or "").strip())
                    params = {"msg": "Mapping initialized from industry template."}
                elif action == "approve_field":
                    mapping_service.approve_field(
                        settings,
                        entity_id=entity_id,
                        field_id=str(form.get("field_id") or "").strip(),
                        silver_column=str(form.get("silver_column") or "").strip() or None,
                    )
                    params = {"msg": "Field approved.", "entity": entity_id}
                elif action == "reject_field":
                    mapping_service.reject_field(
                        settings, entity_id=entity_id, field_id=str(form.get("field_id") or "").strip()
                    )
                    params = {"msg": "Field rejected.", "entity": entity_id}
                elif action == "approve_all":
                    mapping_service.approve_all(settings, entity_id=entity_id or None)
                    params = {"msg": "Approved all suggested fields.", "entity": entity_id}
                elif action == "reject_all":
                    mapping_service.reject_all(settings, entity_id=entity_id or None)
                    params = {"msg": "Rejected all fields.", "entity": entity_id}
                elif action == "exclude_entity":
                    mapping_service.exclude_entity(settings, entity_id=entity_id)
                    params = {"msg": f"Excluded entity {entity_id}."}
                elif action == "exclude_field":
                    mapping_service.exclude_field(
                        settings, entity_id=entity_id, field_id=str(form.get("field_id") or "").strip()
                    )
                    params = {"msg": "Field removed.", "entity": entity_id}
                elif action == "add_entity":
                    mapping_service.add_custom_entity(
                        settings,
                        entity_id=entity_id,
                        silver_source=str(form.get("silver_source") or "").strip(),
                        silver_entity=str(form.get("silver_entity") or "").strip(),
                        field_id=str(form.get("field_id") or "").strip(),
                        silver_column=str(form.get("silver_column") or "").strip(),
                    )
                    params = {"msg": "Custom entity added."}
                elif action == "add_field":
                    mapping_service.add_custom_field(
                        settings,
                        entity_id=entity_id,
                        field_id=str(form.get("field_id") or "").strip(),
                        silver_column=str(form.get("silver_column") or "").strip(),
                    )
                    params = {"msg": "Custom field added.", "entity": entity_id}
                elif action == "promote":
                    result = mapping_service.promote(settings, version=str(form.get("version") or "").strip())
                    report = result["report"]
                    included = ", ".join(report["included_kpis"]) or "none"
                    skipped = "; ".join(
                        f"{item['kpi_id']} ({item['reason']})" for item in report["skipped_kpis"]
                    ) or "none"
                    params = {
                        "msg": (
                            f"Promoted {result['pack'].pack_id} v{result['pack'].version} — "
                            f"included: {included} — skipped: {skipped}"
                        )
                    }
                else:
                    params = {"err": f"Unknown action {action!r}"}
            except ValueError as exc:
                params = {"err": str(exc)}
                if entity_id:
                    params["entity"] = entity_id
            return RedirectResponse(f"/dna/model-mapping?{urlencode(params)}", status_code=302)

        response = render_model_mapping(
            REQUEST,
            settings=settings,
            client=client,
            is_admin=is_admin,
            entity=entity_param,
            message=str(request.query_params.get("msg") or ""),
            error=str(request.query_params.get("err") or ""),
        )
        return to_starlette_response(response)


def register_source_docs_routes(app: FastAPI) -> None:
    from hiveflow.dna.web.portal.views import render_source_docs_inspector

    async def _render(request: Request, source: str | None):
        settings, client, is_admin = _ctx(request)
        response = render_source_docs_inspector(
            REQUEST,
            settings=settings,
            client=client,
            is_admin=is_admin,
            message=str(request.query_params.get("msg") or ""),
            error=str(request.query_params.get("err") or ""),
            source=source,
            configured_sources=_configured_reference_sources(settings),
        )
        return to_starlette_response(response)

    @app.get("/semantics/source-docs")
    async def source_docs_inspector(request: Request):
        return await _render(request, None)

    @app.get("/semantics/source-docs/{source}")
    async def source_docs_inspector_source(request: Request, source: str):
        return await _render(request, source)

    @app.get("/api/source-docs-gold")
    async def api_source_docs_gold(request: Request):
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_gold_status

        settings, _client, _is_admin = _ctx(request)
        source = str(request.query_params.get("source") or "").strip() or None
        return source_docs_gold_status(settings, source=source)

    @app.post("/api/source-docs-gold/build")
    async def api_source_docs_gold_build(request: Request):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json_forbidden()
        from hiveflow.dna_engine.web.auth import hosting_company
        from hiveflow.dna_engine.web.tenant import hosting_environment
        from hiveflow.dna.web.portal.semantics.source_docs_service import enqueue_source_docs_gold_build

        body = await _json_body(request)
        source = str(body.get("source") or request.query_params.get("source") or "").strip() or None
        try:
            result = enqueue_source_docs_gold_build(
                settings,
                company=hosting_company(),
                environment=hosting_environment(),
                source=source,
                seed_missing_overlays=bool(body.get("seed_missing_overlays", True)),
                publish_schemas=bool(body.get("publish_schemas", False)),
            )
            status_code = 500 if result.get("status") == "error" else 200
            return _json(result, status_code)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc)}, 500)

    @app.post("/api/source-docs-gold/exclude")
    async def api_source_docs_gold_exclude(request: Request):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json_forbidden()
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_exclude

        body = await _json_body(request)
        try:
            return _json(source_docs_exclude(settings, body), 200)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc)}, 500)

    @app.post("/api/source-docs-gold/undo-exclude")
    async def api_source_docs_gold_undo_exclude(request: Request):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json_forbidden()
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_undo_exclude

        body = await _json_body(request)
        try:
            return _json(source_docs_undo_exclude(settings, body), 200)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc)}, 500)

    @app.post("/api/source-docs-gold/submit")
    async def api_source_docs_gold_submit(request: Request):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json_forbidden()
        from hiveflow.dna_engine.web.auth import hosting_company
        from hiveflow.dna_engine.web.tenant import hosting_environment
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_submit_changes

        body = await _json_body(request)
        source = str(body.get("source") or request.query_params.get("source") or "").strip() or None
        raw_excludes = body.get("excludes")
        excludes = raw_excludes if isinstance(raw_excludes, list) else None
        try:
            result = source_docs_submit_changes(
                settings,
                company=hosting_company(),
                environment=hosting_environment(),
                source=source,
                excludes=excludes,
            )
            status_code = 200
            if result.get("status") == "error":
                status_code = 400 if result.get("reason") == "no_pending" else 500
            return _json(result, status_code)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc)}, 500)

    @app.get("/api/source-docs-gold/versions")
    async def api_source_docs_gold_versions(request: Request):
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_versions

        settings, _client, _is_admin = _ctx(request)
        source = str(request.query_params.get("source") or "").strip() or None
        return source_docs_versions(settings, source=source)

    @app.post("/api/source-docs-gold/versions/commit")
    async def api_source_docs_gold_versions_commit(request: Request):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json_forbidden()
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_commit_version

        body = await _json_body(request)
        source = str(body.get("source") or request.query_params.get("source") or "").strip() or None
        note = str(body.get("note") or "Submitted").strip() or "Submitted"
        try:
            return _json(source_docs_commit_version(settings, source=source, note=note), 200)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc)}, 500)

    @app.post("/api/source-docs-gold/restore")
    async def api_source_docs_gold_restore(request: Request):
        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json_forbidden()
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_restore_version

        body = await _json_body(request)
        source = str(body.get("source") or request.query_params.get("source") or "").strip() or None
        version = str(body.get("version") or "").strip()
        try:
            return _json(source_docs_restore_version(settings, source=source, version=version), 200)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": str(exc)}, 500)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — matches werkzeug get_json(silent=True)
        return {}
    return body if isinstance(body, dict) else {}


def _json(payload: dict, status_code: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(payload, status_code=status_code)


def _json_forbidden():
    return _json({"error": "forbidden"}, 403)


def register_governance_routes(app: FastAPI) -> None:
    from hiveflow.dna.web.portal.governance_restore import restore_governance_target
    from hiveflow.dna.web.portal.views import render_admin_users, render_governance

    @app.api_route("/governance", methods=["GET", "POST"])
    async def governance(request: Request):
        settings, client, is_admin = _ctx(request)
        message = str(request.query_params.get("msg") or "")
        error = str(request.query_params.get("err") or "")
        session = request.state.session

        if request.method == "POST":
            if not is_admin:
                return PlainTextResponse("Forbidden", status_code=403)
            form = await request.form()
            action = str(form.get("action", "")).strip()
            try:
                if action in {"restore_dna", "restore_reporting"}:
                    target = "dna" if action == "restore_dna" else "reporting"
                    source_version = str(form.get("source_version", "")).strip()
                    result = restore_governance_target(
                        settings, target=target, source_version=source_version, username=session.username
                    )
                    label = "DNA" if target == "dna" else "reporting"
                    message = (
                        f"Restored {label} from v{result['restored_from']} "
                        f"as v{result['version']} and pinned production. "
                        "Gold outputs are unchanged until compile and publish."
                    )
                    return RedirectResponse(f"/governance?{urlencode({'msg': message})}", status_code=302)
                raise ValueError(f"Unknown action {action!r}")
            except Exception as exc:  # noqa: BLE001 — surface governance errors in UI
                error = str(exc)

        response = render_governance(
            REQUEST, settings=settings, client=client, is_admin=is_admin, message=message, error=error
        )
        return to_starlette_response(response)

    @app.api_route("/governance/users", methods=["GET", "POST"])
    async def governance_users(request: Request):
        from hiveflow.dna.web.portal.cognito import (
            PORTAL_ROLE_MEMBER,
            PortalUserAlreadyExists,
            PortalUserLimitExceeded,
            cognito_configured,
            invite_portal_user,
            list_portal_users_for_client,
            set_portal_user_role,
        )
        from hiveflow.dna_engine.web.auth import hosting_company
        from hiveflow.dna_engine.web.tenant import hosting_environment

        settings, client, is_admin = _ctx(request)
        session = request.state.session
        if not is_admin:
            return PlainTextResponse(
                "Admin access required for Users. Your Cognito user needs custom:portal_role=admin.",
                status_code=403,
            )

        message = ""
        error = ""
        invites_enabled = cognito_configured()
        company = hosting_company()
        environment = hosting_environment()

        if request.method == "POST":
            form = await request.form()
            action = str(form.get("action", "")).strip()
            if action == "invite":
                if not invites_enabled:
                    error = "User invites require Cognito authentication."
                else:
                    username = str(form.get("username", "")).strip()
                    email = str(form.get("email", "")).strip()
                    role = str(form.get("role", PORTAL_ROLE_MEMBER)).strip()
                    try:
                        invite_portal_user(
                            username=username,
                            client_id=session.client_id,
                            email=email,
                            company=company,
                            environment=environment,
                            max_users=client.max_users,
                            role=role,
                        )
                        message = f"Invite sent to {email} as {role}."
                    except PortalUserLimitExceeded:
                        error = f"Seat limit reached ({client.max_users} users)."
                    except PortalUserAlreadyExists:
                        error = f"Username {username!r} is already taken."
                    except (ValueError, RuntimeError) as exc:
                        error = str(exc)
            elif action == "set_role":
                if not invites_enabled:
                    error = "Role changes require Cognito authentication."
                else:
                    username = str(form.get("username", "")).strip()
                    role = str(form.get("role", PORTAL_ROLE_MEMBER)).strip()
                    if username.casefold() == session.username.strip().casefold():
                        error = "You cannot change your own role."
                    else:
                        try:
                            set_portal_user_role(
                                username=username, role=role, company=company, environment=environment
                            )
                            message = f"Updated {username} to {role}."
                        except (ValueError, RuntimeError) as exc:
                            error = str(exc)

        if invites_enabled:
            users = list_portal_users_for_client(
                client_id=session.client_id, company=company, environment=environment
            )
        else:
            from hiveflow.dna.web.portal.views import _legacy_portal_users

            users = _legacy_portal_users(session.client_id, company=company, environment=environment)

        response = render_admin_users(
            REQUEST,
            client=client,
            users=users,
            current_username=session.username,
            message=message,
            error=error,
            invites_enabled=invites_enabled,
            is_admin=True,
            settings=settings,
        )
        return to_starlette_response(response)

    @app.get("/governance/config")
    async def governance_config_redirect(request: Request):
        return RedirectResponse("/governance", status_code=302)

    @app.get("/governance/config/preview/exit")
    async def governance_config_preview_exit(request: Request):
        from hiveflow.dna.web.portal.preview import clear_preview_cookie

        _settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return PlainTextResponse("Forbidden", status_code=403)
        response = RedirectResponse("/governance", status_code=302)
        clear_preview_cookie(response)
        return response


def _kpi_generator_redirect(*, proposal_id: str = "", message: str = "", error: str = ""):
    """Post/Redirect/Get for KPI Generator; keep viewport on validation results."""
    params: dict[str, str] = {"validated": "1"}
    if proposal_id:
        params["proposal_id"] = proposal_id
    if message:
        params["msg"] = message
    if error:
        params["err"] = error
    return RedirectResponse(
        f"/dna/kpi-generator?{urlencode(params)}#kpi-generator-validation", status_code=302
    )


def register_kpi_generator_routes(app: FastAPI) -> None:
    from hiveflow.dna_engine.web.tenant import hosting_environment

    @app.api_route("/dna/kpi-generator", methods=["GET", "POST"])
    async def kpi_generator(request: Request):
        from hiveflow.dna.web.portal.dna_manual_refresh import (
            gold_refresh_status,
            quota_summary as manual_refresh_quota_summary,
            trigger_manual_refresh,
        )
        from hiveflow.dna.web.portal.governance_helpers.bedrock_usage import BedrockBudgetExceeded
        from hiveflow.dna.web.portal.kpi_generator.catalog import (
            parse_validation_filters,
            validation_criteria_from_proposal,
        )
        from hiveflow.dna.web.portal.kpi_generator.generation import (
            close_working_kpi_proposals,
            enqueue_kpi_generation,
            load_kpi_generator_workspace,
            load_kpi_proposal,
        )
        from hiveflow.dna.web.portal.kpi_generator.governance import (
            approve_all_kpi_drafts,
            approve_kpi_draft_group,
            approve_kpi_proposal,
            discard_kpi_proposal,
            publish_all_approved_kpis,
            reject_all_kpi_drafts,
            reject_kpi_draft_group,
            reject_kpi_proposal,
            run_validation,
            save_kpi_governance_draft,
            save_validation_criteria,
            update_kpi_draft_sql,
            validate_kpi_draft_group,
        )
        from hiveflow.dna.web.portal.kpi_generator.drafts import proposal_generation_status
        from hiveflow.dna.web.portal.views import render_kpi_generator
        from hiveflow.dna.workflow import load_production_pack, load_workflow_state

        settings, client, is_admin = _ctx(request)
        session = request.state.session
        environment = hosting_environment()

        message = str(request.query_params.get("msg") or "")
        error = str(request.query_params.get("err") or "")
        active_tab = str(request.query_params.get("tab") or "generator").strip().lower()
        if active_tab not in {"generator", "review"}:
            active_tab = "generator"
        proposal = None
        validation = None
        pending_drafts: list = []
        approved_drafts: list = []
        proposal_id = str(request.query_params.get("proposal_id") or "").strip()
        workspace_working = None
        if is_admin and request.method != "POST":
            workspace_working, pending_drafts, approved_drafts = load_kpi_generator_workspace(settings)
        if proposal_id:
            loaded = load_kpi_proposal(settings, proposal_id)
            if loaded and str(loaded.get("status") or "").strip().lower() == "working":
                proposal = loaded
        elif request.method != "POST" and is_admin:
            proposal = workspace_working
            if proposal:
                proposal_id = str(proposal.get("proposal_id") or "").strip()
        if proposal_generation_status(proposal) == "error":
            error = error or str(proposal.get("generation_error") or "KPI generation failed.")

        if request.method == "POST":
            if not is_admin:
                return PlainTextResponse("Forbidden", status_code=403)
            form = await request.form()
            action = str(form.get("action", "")).strip()
            try:
                if action == "generate":
                    prior_proposal_id = str(form.get("prior_proposal_id") or "").strip()
                    prior_chat_history: list[dict[str, str]] | None = None
                    prior_validation_criteria = None
                    if prior_proposal_id:
                        prior = load_kpi_proposal(settings, prior_proposal_id)
                        if prior and str(prior.get("status") or "").strip().lower() == "working":
                            prior_chat_history = prior.get("chat_history") or []
                            prior_validation_criteria = validation_criteria_from_proposal(prior)
                    proposal = enqueue_kpi_generation(
                        settings,
                        prompt=str(form.get("prompt") or ""),
                        client_id=client.client_id,
                        monthly_budget_usd=client.config_assistant_monthly_budget_usd,
                        username=session.username,
                        prior_chat_history=prior_chat_history,
                        prior_validation_criteria=prior_validation_criteria,
                        prior_proposal_id=prior_proposal_id,
                    )
                    params = {"proposal_id": proposal["proposal_id"]}
                    if proposal_generation_status(proposal) != "pending":
                        params["msg"] = "Draft generated. Validate, then save as a DNA draft for review."
                    return RedirectResponse(f"/dna/kpi-generator?{urlencode(params)}", status_code=302)
                elif action == "validate":
                    proposal_id = str(form.get("proposal_id") or "").strip()
                    try:
                        sql = str(form.get("sql") or "").strip()
                        sql_by_layer = {
                            "silver": str(form.get("sql_silver") or "").strip(),
                            "gold": str(form.get("sql_gold") or "").strip(),
                        }
                        if any(sql_by_layer.values()) or sql:
                            update_kpi_draft_sql(
                                settings, proposal_id=proposal_id, sql=sql, sql_by_layer=sql_by_layer
                            )
                        filters = parse_validation_filters(
                            form.getlist("filter_fact"),
                            form.getlist("filter_field"),
                            form.getlist("filter_value"),
                        )
                        save_validation_criteria(settings, proposal_id=proposal_id, filters=filters)
                        run_validation(
                            settings,
                            proposal_id=proposal_id,
                            filters=filters,
                            company=settings.company,
                            environment=environment,
                        )
                        return _kpi_generator_redirect(
                            proposal_id=proposal_id, message="Validation query completed."
                        )
                    except BedrockBudgetExceeded as exc:
                        return _kpi_generator_redirect(
                            proposal_id=proposal_id,
                            error=(
                                f"Monthly Bedrock allowance reached "
                                f"(${exc.estimated_cost_usd:.2f} / ${exc.monthly_budget_usd:.2f})."
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001
                        if proposal_id:
                            try:
                                filters = parse_validation_filters(
                                    form.getlist("filter_fact"),
                                    form.getlist("filter_field"),
                                    form.getlist("filter_value"),
                                )
                                save_validation_criteria(settings, proposal_id=proposal_id, filters=filters)
                            except Exception:  # noqa: BLE001
                                pass
                        return _kpi_generator_redirect(proposal_id=proposal_id, error=str(exc))
                elif action == "save_draft":
                    proposal_id = str(form.get("proposal_id") or "").strip()
                    sql = str(form.get("sql") or "").strip()
                    sql_by_layer = {
                        "silver": str(form.get("sql_silver") or "").strip(),
                        "gold": str(form.get("sql_gold") or "").strip(),
                    }
                    if any(sql_by_layer.values()) or sql:
                        update_kpi_draft_sql(
                            settings, proposal_id=proposal_id, sql=sql, sql_by_layer=sql_by_layer
                        )
                    result = save_kpi_governance_draft(settings, proposal_id=proposal_id, username=session.username)
                    message = (
                        f"Saved DNA draft v{result['version']} ({result['sql_file']}). "
                        "Review it on the Review Drafts tab."
                    )
                    close_working_kpi_proposals(settings, username=session.username)
                    return RedirectResponse(
                        f"/dna/kpi-generator?{urlencode({'tab': 'review', 'msg': message})}", status_code=302
                    )
                elif action == "discard_draft":
                    proposal_id = str(form.get("proposal_id") or "").strip()
                    if proposal_id:
                        discard_kpi_proposal(settings, proposal_id=proposal_id, username=session.username)
                    close_working_kpi_proposals(settings, username=session.username)
                    return RedirectResponse(
                        f"/dna/kpi-generator?{urlencode({'msg': 'Draft discarded.'})}", status_code=302
                    )
                elif action == "validate_integrity":
                    target_key = str(form.get("target_key") or "").strip()
                    proposal_ids = [str(pid).strip() for pid in form.getlist("proposal_ids") if str(pid).strip()]
                    validation = validate_kpi_draft_group(
                        settings,
                        target_key=target_key,
                        proposal_ids=proposal_ids,
                        company=settings.company,
                        environment=environment,
                    )
                    if str(validation.get("status") or "").strip().lower() == "passed":
                        message = f"Integrity validation passed for {target_key}."
                    else:
                        errors = validation.get("errors") or ["Integrity validation failed"]
                        error = "; ".join(str(err) for err in errors)
                    active_tab = "review"
                elif action == "approve_group":
                    target_key = str(form.get("target_key") or "").strip()
                    proposal_ids = [str(pid).strip() for pid in form.getlist("proposal_ids") if str(pid).strip()]
                    next_version = str(form.get("next_sql_version") or "").strip() or None
                    result = approve_kpi_draft_group(
                        settings,
                        target_key=target_key,
                        proposal_ids=proposal_ids,
                        username=session.username,
                        version=next_version,
                        company=settings.company,
                        environment=environment,
                    )
                    message = (
                        f"Approved {len(result.get('approved') or [])} draft(s) in group "
                        f"{target_key}. Move to Publish to materialize tables."
                    )
                    active_tab = "review"
                elif action == "publish_approved":
                    result = publish_all_approved_kpis(
                        settings,
                        client_id=client.client_id,
                        username=session.username,
                        company=settings.company,
                        environment=environment,
                        monthly_limit=client.dna_manual_refresh_monthly_limit,
                    )
                    published_count = len(result.get("published") or [])
                    message = f"Started DNA refresh for {published_count} approved KPI(s)."
                    active_tab = "review"
                elif action == "reject_group":
                    target_key = str(form.get("target_key") or "").strip()
                    proposal_ids = [str(pid).strip() for pid in form.getlist("proposal_ids") if str(pid).strip()]
                    result = reject_kpi_draft_group(
                        settings, target_key=target_key, proposal_ids=proposal_ids, username=session.username
                    )
                    message = f"Rejected {len(result.get('rejected') or [])} draft(s) in group {target_key}."
                    active_tab = "review"
                elif action == "approve":
                    proposal_id = str(form.get("proposal_id") or "").strip()
                    next_version = str(form.get("next_sql_version") or "").strip() or None
                    result = approve_kpi_proposal(
                        settings,
                        proposal_id=proposal_id,
                        username=session.username,
                        version=next_version,
                        company=settings.company,
                        environment=environment,
                    )
                    message = (
                        f"Approved and pinned SQL pack v{result['version']} "
                        f"({result['sql_file']}). Publish from Review Drafts to materialize."
                    )
                    proposal = load_kpi_proposal(settings, proposal_id)
                    active_tab = "review"
                elif action == "reject":
                    proposal_id = str(form.get("proposal_id") or "").strip()
                    result = reject_kpi_proposal(settings, proposal_id=proposal_id, username=session.username)
                    if str(result.get("prior_status") or "") == "approved":
                        message = f"Removed {proposal_id} from the ready-to-publish queue."
                    else:
                        message = f"Rejected draft {proposal_id}."
                    active_tab = "review"
                elif action == "approve_all":
                    results = approve_all_kpi_drafts(settings, username=session.username)
                    message = f"Approved {len(results)} KPI draft(s) to production."
                    active_tab = "review"
                elif action == "reject_all":
                    results = reject_all_kpi_drafts(settings, username=session.username)
                    message = f"Rejected {len(results)} KPI draft(s)."
                    active_tab = "review"
                elif action == "manual_dna_refresh":
                    workflow = load_workflow_state(settings, settings.dna_config_id)
                    pinned_version = str(workflow.get("active_version") or "").strip()
                    if not pinned_version:
                        try:
                            pinned_version = str(load_production_pack(settings).version or "").strip()
                        except Exception:  # noqa: BLE001
                            pinned_version = ""
                    if not pinned_version:
                        raise ValueError("No production DNA version is pinned yet.")
                    reporting_company = str(client.reporting_company or "").strip() or settings.company
                    result = trigger_manual_refresh(
                        settings,
                        client_id=client.client_id,
                        username=session.username,
                        pinned_version=pinned_version,
                        company=reporting_company,
                        environment=environment,
                        monthly_limit=client.dna_manual_refresh_monthly_limit,
                    )
                    remaining = int((result.get("quota") or {}).get("remaining") or 0)
                    message = (
                        "DNA refresh started. Silver and gold tables will update when the run "
                        f"completes. {remaining} manual refresh(es) remaining this month."
                    )
                else:
                    error = f"Unknown action {action!r}"
            except BedrockBudgetExceeded as exc:
                error = (
                    f"Monthly Bedrock allowance reached "
                    f"(${exc.estimated_cost_usd:.2f} / ${exc.monthly_budget_usd:.2f})."
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            if is_admin:
                _, pending_drafts, approved_drafts = load_kpi_generator_workspace(settings)

        refresh_status = None
        refresh_quota = None
        if is_admin:
            workflow = load_workflow_state(settings, settings.dna_config_id)
            pinned_version = str(workflow.get("active_version") or "").strip()
            if not pinned_version:
                try:
                    pinned_version = str(load_production_pack(settings).version or "").strip()
                except Exception:  # noqa: BLE001
                    pinned_version = ""
            refresh_status = gold_refresh_status(settings, pinned_version=pinned_version).to_dict()
            refresh_quota = manual_refresh_quota_summary(
                settings, client_id=client.client_id, monthly_limit=client.dna_manual_refresh_monthly_limit
            ).to_dict()

        response = render_kpi_generator(
            REQUEST,
            settings=settings,
            client=client,
            is_admin=is_admin,
            proposal=proposal,
            validation=validation,
            message=message,
            error=error,
            active_tab=active_tab,
            pending_drafts=pending_drafts,
            approved_drafts=approved_drafts,
            refresh_status=refresh_status,
            refresh_quota=refresh_quota,
        )
        return to_starlette_response(response)

    @app.get("/dna/kpi-generator/status")
    async def kpi_generator_status(request: Request):
        from hiveflow.dna.web.portal.kpi_generator.drafts import proposal_generation_status
        from hiveflow.dna.web.portal.kpi_generator.generation import load_kpi_proposal

        settings, _client, is_admin = _ctx(request)
        if not is_admin:
            return _json({"error": "forbidden"}, 403)
        proposal_id = str(request.query_params.get("proposal_id") or "").strip()
        if not proposal_id:
            return _json({"error": "proposal_id required"}, 400)
        proposal = load_kpi_proposal(settings, proposal_id) or {}
        gen_status = proposal_generation_status(proposal) or "complete"
        return {
            "proposal_id": proposal_id,
            "generation_status": gen_status,
            "error": str(proposal.get("generation_error") or ""),
        }


def register_routes(app: FastAPI) -> None:
    register_catalog_routes(app)
    register_data_profile_routes(app)
    register_model_mapping_routes(app)
    register_source_docs_routes(app)
    register_governance_routes(app)
    register_kpi_generator_routes(app)
