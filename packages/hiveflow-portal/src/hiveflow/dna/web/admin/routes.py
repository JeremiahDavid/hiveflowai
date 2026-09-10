"""Admin portal route registration — extracted from app.py (Phase 1 split).

Self-contained: verified to depend only on `company`/`environment` (the
deploy-target closure vars from create_app), never on portal internals or on
settings/env_config/ui_mode. Auth (require_portal_session/login_response/
clear_session_cookie) is the one legitimate shared dependency, imported from
portal.auth's public API — not its private internals.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import quote, urlencode

from werkzeug.routing import Rule
from werkzeug.wrappers import Request, Response

from hiveflow.dna.web.portal.auth import (
    clear_session_cookie,
    login_response,
    require_portal_session,
)
from hiveflow.dna.web.routing_helpers import (
    _app_url,
    _json_response,
    _redirect,
    _request_wants_json,
)

ADMIN_UI_ENDPOINTS = frozenset(
    {
        "admin_home",
        "admin_login",
        "admin_logout",
        "admin_architecture",
        "admin_job_run",
        "admin_job_status",
        "admin_onboarding",
        "admin_onboarding_new",
        "admin_onboarding_detail",
        "admin_onboarding_deploy",
        "admin_onboarding_deploy_status",
        "admin_onboarding_invite_admin",
        "admin_onboarding_pipelines",
        "admin_onboarding_pipelines_status",
        "admin_onboarding_pipelines_ingest",
        "admin_onboarding_pipelines_dna",
        "admin_onboarding_pipelines_ingest_report",
        "admin_onboarding_secrets",
        "admin_onboarding_validate",
        "admin_onboarding_dbc_companies",
        "admin_onboarding_qwc",
        "static",
    }
)


def build_admin_routes(
    *, company: str, environment: str
) -> tuple[list[Rule], dict[str, Callable[..., Response]]]:
    """Build the admin-portal Rule list and endpoint dispatch table."""

    def _admin_authorized(request: Request):
        from hiveflow.dna.web.admin.auth import is_platform_admin

        session, redirect = require_portal_session(
            request,
            company=company,
            environment=environment,
            login_url=_app_url(request, "/admin/login"),
        )
        if session is None:
            return None, redirect
        if not is_platform_admin(session.username, company=company, environment=environment):
            response = _redirect(request, "/admin/login")
            clear_session_cookie(response)
            return None, response
        return session, None

    def on_admin_login(request: Request) -> Response:
        from hiveflow.dna.web.admin.auth import authenticate_admin, complete_admin_new_password
        from hiveflow.dna.web.admin.views import render_admin_login_page
        from hiveflow.dna.web.portal.cognito import cognito_configured

        url = lambda path: _app_url(request, path)
        next_path = request.values.get("next", "/admin") or "/admin"
        if not next_path.startswith("/admin"):
            next_path = "/admin"

        if request.method == "GET":
            return Response(
                render_admin_login_page(url=url, next_path=next_path),
                mimetype="text/html",
            )

        mode = str(request.form.get("mode") or "login").strip()
        if mode == "set_password":
            username = str(request.form.get("username") or "")
            session_token = str(request.form.get("session") or "")
            new_password = str(request.form.get("new_password") or "")
            user = complete_admin_new_password(
                username=username,
                session=session_token,
                new_password=new_password,
                company=company,
                environment=environment,
            )
            if user is None:
                return Response(
                    render_admin_login_page(
                        url=url,
                        error="Could not set password. Try again.",
                        next_path=next_path,
                        mode="set_password",
                        username=username,
                        session=session_token,
                    ),
                    mimetype="text/html",
                    status=401,
                )
            return login_response(
                user,
                company=company,
                environment=environment,
                redirect_to=_app_url(request, next_path),
            )

        username = str(request.form.get("username") or "")
        password = str(request.form.get("password") or "")
        if not cognito_configured():
            return Response(
                render_admin_login_page(
                    url=url,
                    error="Admin Cognito is not configured.",
                    next_path=next_path,
                ),
                mimetype="text/html",
                status=503,
            )
        login_result = authenticate_admin(
            username,
            password,
            company=company,
            environment=environment,
        )
        if login_result is None:
            return Response(
                render_admin_login_page(
                    url=url,
                    error="Invalid username or password.",
                    next_path=next_path,
                    username=username,
                ),
                mimetype="text/html",
                status=401,
            )
        if login_result.kind == "new_password" and login_result.challenge is not None:
            challenge = login_result.challenge
            return Response(
                render_admin_login_page(
                    url=url,
                    next_path=next_path,
                    mode="set_password",
                    username=challenge.username,
                    session=challenge.session,
                ),
                mimetype="text/html",
            )
        if login_result.user is None:
            return Response(
                render_admin_login_page(
                    url=url,
                    error="Invalid username or password.",
                    next_path=next_path,
                ),
                mimetype="text/html",
                status=401,
            )
        return login_response(
            login_result.user,
            company=company,
            environment=environment,
            redirect_to=_app_url(request, next_path),
        )

    def on_admin_logout(request: Request) -> Response:
        response = _redirect(request, "/admin/login")
        clear_session_cookie(response)
        return response

    def on_admin_home(request: Request) -> Response:
        from hiveflow.dna.web.admin.jobs import admin_jobs_status_snapshot
        from hiveflow.dna.web.admin.views import render_admin_dashboard

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        flash_raw = request.args.get("flash", "")
        job_id = request.args.get("job", "")
        invoked = str(request.args.get("invoked") or "").strip().lower() in {"1", "true", "yes"}
        flash_by_job = {job_id: flash_raw} if job_id and flash_raw else {}
        optimistic_by_job = {job_id: "queued"} if job_id and invoked else {}
        return Response(
            render_admin_dashboard(
                url=lambda path: _app_url(request, path),
                username=session.username,
                statuses=admin_jobs_status_snapshot(),
                flash_by_job=flash_by_job,
                optimistic_by_job=optimistic_by_job,
            ),
            mimetype="text/html",
        )

    def on_admin_architecture(request: Request) -> Response:
        from hiveflow.dna.web.admin.views import render_admin_architecture

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        return Response(
            render_admin_architecture(
                url=lambda path: _app_url(request, path),
                username=session.username,
            ),
            mimetype="text/html",
        )

    def on_admin_job_run(request: Request, job_id: str) -> Response:
        from hiveflow.dna.web.admin.jobs import (
            AdminJobMisconfigured,
            UnknownAdminJob,
            enqueue_admin_job,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        try:
            result = enqueue_admin_job(job_id)
        except UnknownAdminJob:
            return Response("Unknown job", status=404)
        except AdminJobMisconfigured as exc:
            return Response(str(exc), status=503)
        status = str(result.get("status") or "queued")
        if status == "queued":
            flash = "Invoked — badge will move to Running, then Completed or Failed."
        elif status == "dry_run":
            flash = "Dry run only (local) — Lambda was not invoked."
        else:
            flash = f"Invoke returned status {status}."
        if result.get("follow_ons"):
            flash += f" Follow-ons: {', '.join(result['follow_ons'])}."
        params = {"job": job_id, "flash": flash}
        if status == "queued":
            params["invoked"] = "1"
        return _redirect(request, f"/admin?{urlencode(params)}")

    def on_admin_job_status(request: Request, job_id: str) -> Response:
        from hiveflow.dna.web.admin.jobs import (
            AdminJobMisconfigured,
            UnknownAdminJob,
            admin_job_status,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        try:
            payload = admin_job_status(job_id)
        except UnknownAdminJob:
            return Response("Unknown job", status=404)
        except AdminJobMisconfigured as exc:
            return Response(str(exc), status=503)
        return _json_response(payload)

    def on_admin_onboarding(request: Request) -> Response:
        from hiveflow.dna.web.admin.onboarding import list_onboarding_clients, render_onboarding_home

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        flash = str(request.args.get("flash", ""))
        return Response(
            render_onboarding_home(
                url=lambda path: _app_url(request, path),
                username=session.username,
                clients=list_onboarding_clients(),
                flash=flash,
            ),
            mimetype="text/html",
        )

    def on_admin_onboarding_new(request: Request) -> Response:
        from hiveflow.dna.web.admin.onboarding import render_onboarding_wizard, save_client_from_form
        from hiveflow.dna.web.admin.onboarding.handlers import (
            client_config_form_values,
            validate_client_config_form,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        url = lambda path: _app_url(request, path)
        if request.method == "GET":
            company = str(request.args.get("company", "")).strip().lower()
            environment = str(request.args.get("environment", "dev")).strip().lower()
            client_id = str(request.args.get("client_id", "")).strip().lower()
            form_values: dict[str, str] = {}
            if company and client_id:
                try:
                    form_values = client_config_form_values(
                        company=company,
                        environment=environment,
                        client_id=client_id,
                    )
                except Exception as exc:
                    return Response(str(exc), status=404)
            return Response(
                render_onboarding_wizard(
                    url=url,
                    username=session.username,
                    form_values=form_values,
                    company=company,
                    environment=environment,
                    client_id=client_id,
                ),
                mimetype="text/html",
            )

        form = {key: str(value) for key, value in request.form.items()}
        try:
            validate_client_config_form(form)
            result = save_client_from_form(form)
        except Exception as exc:
            return Response(
                render_onboarding_wizard(
                    url=url,
                    username=session.username,
                    form_values=form,
                    error=str(exc),
                    company=str(form.get("onboarding_company", "")).strip().lower(),
                    environment=str(form.get("onboarding_environment", "")).strip().lower(),
                    client_id=str(form.get("onboarding_client_id", "")).strip().lower(),
                ),
                mimetype="text/html",
                status=400,
            )
        client = result["client"]
        return _redirect(
            request,
            f"/admin/onboarding/{client['company'].lower()}?environment={client['environment']}&client_id={client['client_id']}&flash=Client+config+saved",
        )

    def _onboarding_company_context(request: Request, company: str) -> tuple[str, str, str]:
        environment = str(request.values.get("environment", "dev")).strip().lower()
        client_id = str(request.values.get("client_id", "")).strip().lower()
        return company.strip().lower(), environment, client_id

    def on_admin_onboarding_detail(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import (
            get_onboarding_client,
            load_client_connector_credentials,
            render_connector_credentials,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return Response("Client not found", status=404)
        connector_credentials = load_client_connector_credentials(
            company=record.company,
            environment=record.environment,
            sources=record.connector_sources,
        )
        return Response(
            render_connector_credentials(
                url=lambda path: _app_url(request, path),
                username=session.username,
                company=record.company,
                client_id=record.client_id,
                environment=record.environment,
                connector_sources=record.connector_sources,
                connector_credentials=connector_credentials,
                flash=str(request.args.get("flash", "")),
            ),
            mimetype="text/html",
        )

    def on_admin_onboarding_deploy(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import (
            client_deploy_status,
            connectors_ready_for_deploy,
            client_portal_site_urls,
            get_onboarding_client,
            initial_admin_from_config,
            render_client_deploy,
            portal_deploy_ready,
            portal_dns_required,
            trigger_deploy,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return Response("Client not found", status=404)

        if request.method == "GET":
            readiness = connectors_ready_for_deploy(
                company=record.company,
                environment=record.environment,
                client_id=record.client_id,
            )
            if not readiness.get("ok"):
                flash = str(readiness.get("message") or "Validate all connectors before deploying.")
                return _redirect(
                    request,
                    (
                        f"/admin/onboarding/{company_key.lower()}"
                        f"?environment={environment}&client_id={client_id}&flash={quote(flash)}"
                    ),
                )
            build_id = str(request.args.get("build_id", "")).strip()
            status_payload = client_deploy_status(
                company=record.company,
                environment=record.environment,
                client_id=record.client_id,
                build_id=build_id or None,
            )
            initial_admin = initial_admin_from_config(
                company=record.company,
                environment=record.environment,
                client_id=record.client_id,
            )
            portal_urls = client_portal_site_urls(
                environment=record.environment,
                client_id=record.client_id,
            )
            return Response(
                render_client_deploy(
                    url=lambda path: _app_url(request, path),
                    username=session.username,
                    company=record.company,
                    client_id=record.client_id,
                    environment=record.environment,
                    status_payload=status_payload,
                    flash=str(request.args.get("flash", "")),
                    build_id=build_id,
                    initial_admin=initial_admin,
                    portal_ready=portal_deploy_ready(
                        client_id=record.client_id,
                        environment=record.environment,
                        status_payload=status_payload,
                        company=record.company,
                    ),
                    portal_dns_required=portal_dns_required(environment=record.environment),
                    portal_urls=portal_urls,
                ),
                mimetype="text/html",
            )

        result = trigger_deploy(
            company=record.company,
            environment=record.environment,
            client_id=record.client_id,
        )
        build_id = str(result.get("build_id", ""))
        flash = str(result.get("message") or result.get("status") or "deploy triggered")
        if _request_wants_json(request):
            payload = dict(result)
            payload["message"] = flash
            status = 200 if payload.get("ok") else 400
            return _json_response(payload, status=status)
        params = f"environment={environment}&client_id={client_id}&flash={quote(flash)}"
        if build_id:
            params += f"&build_id={build_id}"
        return _redirect(request, f"/admin/onboarding/{company_key.lower()}/deploy?{params}")

    def on_admin_onboarding_invite_admin(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import (
            client_deploy_status,
            get_onboarding_client,
            invite_onboarding_admin,
        )
        from hiveflow.dna.web.portal.cognito import PortalUserAlreadyExists, PortalUserLimitExceeded

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return Response("Client not found", status=404)

        form = {key: str(value) for key, value in request.form.items()}
        username = str(form.get("initial_admin_username", "")).strip()
        email = str(form.get("initial_admin_email", "")).strip()
        status_payload = client_deploy_status(
            company=record.company,
            environment=record.environment,
            client_id=record.client_id,
        )
        try:
            result = invite_onboarding_admin(
                company=record.company,
                environment=record.environment,
                client_id=record.client_id,
                username=username,
                email=email,
                status_payload=status_payload,
            )
            flash = str(result.get("message") or "Admin invite sent")
        except PortalUserLimitExceeded:
            flash = "Seat limit reached for this client."
        except PortalUserAlreadyExists:
            flash = f"Username {username!r} is already taken."
        except ValueError as exc:
            flash = str(exc)
        except RuntimeError as exc:
            flash = str(exc)
        params = f"environment={environment}&client_id={client_id}&flash={quote(flash)}"
        return _redirect(request, f"/admin/onboarding/{company_key.lower()}/deploy?{params}")

    def on_admin_onboarding_deploy_status(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import client_deploy_status

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        build_id = str(request.args.get("build_id", "")).strip()
        try:
            payload = client_deploy_status(
                company=company_key,
                environment=environment,
                client_id=client_id,
                build_id=build_id or None,
            )
        except ValueError as exc:
            return _json_response({"ok": False, "error": str(exc)}, status=404)
        payload["ok"] = True
        return _json_response(payload)

    def on_admin_onboarding_pipelines(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import (
            client_pipeline_status,
            get_onboarding_client,
            render_client_pipelines,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return Response("Client not found", status=404)
        status_payload = client_pipeline_status(record)
        return Response(
            render_client_pipelines(
                url=lambda path: _app_url(request, path),
                username=session.username,
                company=record.company,
                client_id=record.client_id,
                environment=record.environment,
                connector_sources=record.connector_sources,
                dna_enabled=record.dna_enabled,
                status_payload=status_payload,
                flash=str(request.args.get("flash", "")),
            ),
            mimetype="text/html",
        )

    def on_admin_onboarding_pipelines_status(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import client_pipeline_status, get_onboarding_client

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return _json_response({"ok": False, "error": "Client not found"}, status=404)
        tracked: dict[str, str] = {}
        for key, value in request.args.items():
            if key.startswith("ingest_") and value:
                tracked[f"ingest:{key.removeprefix('ingest_')}"] = str(value)
        dna_execution = str(request.args.get("dna_execution", "")).strip()
        if dna_execution:
            tracked["dna"] = dna_execution
        payload = client_pipeline_status(record, tracked_executions=tracked)
        payload["ok"] = True
        return _json_response(payload)

    def on_admin_onboarding_pipelines_ingest(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import (
            get_onboarding_client,
            trigger_ingest_refresh,
        )

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return _json_response({"ok": False, "error": "Client not found"}, status=404)
        connector = str(request.form.get("connector_source", "")).strip().lower()
        result = trigger_ingest_refresh(record, connector=connector)
        if _request_wants_json(request):
            status = 200 if result.get("ok") else 400
            return _json_response(result, status=status)
        flash = str(result.get("message") or "Ingest refresh triggered")
        return _redirect(
            request,
            (
                f"/admin/onboarding/{company_key.lower()}/pipelines"
                f"?environment={environment}&client_id={client_id}&flash={quote(flash)}"
            ),
        )

    def on_admin_onboarding_pipelines_dna(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import get_onboarding_client, trigger_dna_refresh

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return _json_response({"ok": False, "error": "Client not found"}, status=404)
        result = trigger_dna_refresh(record, username=session.username)
        if _request_wants_json(request):
            status = 200 if result.get("ok") else 400
            return _json_response(result, status=status)
        flash = str(result.get("message") or "DNA refresh triggered")
        return _redirect(
            request,
            (
                f"/admin/onboarding/{company_key.lower()}/pipelines"
                f"?environment={environment}&client_id={client_id}&flash={quote(flash)}"
            ),
        )

    def on_admin_onboarding_pipelines_ingest_report(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import get_onboarding_client, ingest_validation_report

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        record = get_onboarding_client(
            company_key,
            environment=environment,
            client_id=client_id or None,
        )
        if record is None:
            return _json_response({"ok": False, "error": "Client not found"}, status=404)
        connector = str(request.args.get("connector", "")).strip().lower()
        run_id = str(request.args.get("run_id", "")).strip() or None
        payload = ingest_validation_report(record, connector=connector, run_id=run_id)
        status = 200 if payload.get("ok") else 404
        return _json_response(payload, status=status)

    def on_admin_onboarding_secrets(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import save_connector_secret

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        source = str(request.form.get("connector_source", "dbc")).strip().lower()
        credentials = {key: str(value) for key, value in request.form.items() if key.isupper()}
        save_connector_secret(
            company=company_key,
            environment=environment,
            source=source,
            credentials=credentials,
        )
        return _redirect(
            request,
            f"/admin/onboarding/{company_key.lower()}?environment={environment}&client_id={client_id}&flash=Secret+saved",
        )

    def on_admin_onboarding_validate(request: Request, company: str) -> Response:
        from hiveflow.client_registry import ClientRegistry
        from hiveflow.dna.web.admin.onboarding import validate_connector

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        company_key, environment, client_id = _onboarding_company_context(request, company)
        source = str(request.form.get("connector_source", "dbc")).strip().lower()
        credentials = {key: str(value) for key, value in request.form.items() if key.isupper()}
        registry = ClientRegistry()
        record = registry.get_client(company_key, environment=environment, client_id=client_id or None)
        secret_id = registry.secret_name(record, source=source) if record else None
        result = validate_connector(source=source, credentials=credentials, secret_id=secret_id)
        message = str(result.get("message") or result.get("error") or "").strip()
        if result.get("ok") and not message:
            if source == "dbc":
                company_name = str(result.get("company_name") or "").strip().rstrip(".")
                company_id = str(result.get("company_id") or "").strip()
                label = company_name or company_id or "company"
                message = f"Connected to {label}."
            elif source == "qbd":
                message = "QBD credentials and SOAP URL look valid."
            else:
                message = "Connector validated."
        payload = dict(result)
        if message:
            payload["message"] = message
        if _request_wants_json(request):
            status = 200 if result.get("ok") else 400
            return _json_response(payload, status=status)
        flash = message or ("Validation failed." if not result.get("ok") else "Connector validated.")
        return _redirect(
            request,
            (
                f"/admin/onboarding/{company_key.lower()}"
                f"?environment={environment}&client_id={client_id}&flash={quote(flash)}"
                f"#connector-credentials-{source}"
            ),
        )

    def on_admin_onboarding_dbc_companies(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding import list_connector_companies

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        credentials = {key: str(value) for key, value in request.form.items() if key.isupper()}
        result = list_connector_companies(source="dbc", credentials=credentials)
        status = 200 if result.get("ok") else 400
        return _json_response(result, status=status)

    def on_admin_onboarding_qwc(request: Request, company: str) -> Response:
        from hiveflow.dna.web.admin.onboarding.handlers import generate_qwc_download

        session, redirect = _admin_authorized(request)
        if session is None:
            return redirect
        soap_url = str(request.args.get("soap_url", "")).strip()
        username = str(request.args.get("username", "")).strip()
        if not soap_url or not username:
            return Response("soap_url and username are required", status=400)
        xml = generate_qwc_download(soap_url=soap_url, username=username)
        return Response(
            xml,
            mimetype="application/xml",
            headers={"Content-Disposition": 'attachment; filename="hiveflow.qwc"'},
        )

    rules: list[Rule] = [
        Rule("/", endpoint="admin_home"),
        Rule("/admin", endpoint="admin_home"),
        Rule("/admin/", endpoint="admin_home"),
        Rule("/admin/login", endpoint="admin_login", methods=["GET", "POST"]),
        Rule("/admin/logout", endpoint="admin_logout", methods=["GET", "POST"]),
        Rule("/admin/architecture", endpoint="admin_architecture", methods=["GET"]),
        Rule("/admin/onboarding", endpoint="admin_onboarding", methods=["GET"]),
        Rule("/admin/onboarding/new", endpoint="admin_onboarding_new", methods=["GET", "POST"]),
        Rule(
            "/admin/onboarding/<company>/deploy",
            endpoint="admin_onboarding_deploy",
            methods=["GET", "POST"],
        ),
        Rule(
            "/admin/onboarding/<company>/deploy/status",
            endpoint="admin_onboarding_deploy_status",
            methods=["GET"],
        ),
        Rule(
            "/admin/onboarding/<company>/invite-admin",
            endpoint="admin_onboarding_invite_admin",
            methods=["POST"],
        ),
        Rule(
            "/admin/onboarding/<company>/pipelines",
            endpoint="admin_onboarding_pipelines",
            methods=["GET"],
        ),
        Rule(
            "/admin/onboarding/<company>/pipelines/status",
            endpoint="admin_onboarding_pipelines_status",
            methods=["GET"],
        ),
        Rule(
            "/admin/onboarding/<company>/pipelines/ingest",
            endpoint="admin_onboarding_pipelines_ingest",
            methods=["POST"],
        ),
        Rule(
            "/admin/onboarding/<company>/pipelines/dna",
            endpoint="admin_onboarding_pipelines_dna",
            methods=["POST"],
        ),
        Rule(
            "/admin/onboarding/<company>/pipelines/ingest/report",
            endpoint="admin_onboarding_pipelines_ingest_report",
            methods=["GET"],
        ),
        Rule(
            "/admin/onboarding/<company>/secrets",
            endpoint="admin_onboarding_secrets",
            methods=["POST"],
        ),
        Rule(
            "/admin/onboarding/<company>/qwc",
            endpoint="admin_onboarding_qwc",
            methods=["GET"],
        ),
        Rule(
            "/admin/onboarding/<company>/validate",
            endpoint="admin_onboarding_validate",
            methods=["POST"],
        ),
        Rule(
            "/admin/onboarding/<company>/dbc/companies",
            endpoint="admin_onboarding_dbc_companies",
            methods=["POST"],
        ),
        Rule(
            "/admin/onboarding/<company>",
            endpoint="admin_onboarding_detail",
            methods=["GET"],
        ),
        Rule("/admin/jobs/<job_id>/run", endpoint="admin_job_run", methods=["POST"]),
        Rule("/admin/jobs/<job_id>/status", endpoint="admin_job_status", methods=["GET"]),
    ]

    endpoints: dict[str, Callable[..., Response]] = {
        "admin_home": on_admin_home,
        "admin_login": on_admin_login,
        "admin_logout": on_admin_logout,
        "admin_architecture": on_admin_architecture,
        "admin_job_run": on_admin_job_run,
        "admin_job_status": on_admin_job_status,
        "admin_onboarding": on_admin_onboarding,
        "admin_onboarding_new": on_admin_onboarding_new,
        "admin_onboarding_detail": on_admin_onboarding_detail,
        "admin_onboarding_deploy": on_admin_onboarding_deploy,
        "admin_onboarding_deploy_status": on_admin_onboarding_deploy_status,
        "admin_onboarding_invite_admin": on_admin_onboarding_invite_admin,
        "admin_onboarding_pipelines": on_admin_onboarding_pipelines,
        "admin_onboarding_pipelines_status": on_admin_onboarding_pipelines_status,
        "admin_onboarding_pipelines_ingest": on_admin_onboarding_pipelines_ingest,
        "admin_onboarding_pipelines_dna": on_admin_onboarding_pipelines_dna,
        "admin_onboarding_pipelines_ingest_report": on_admin_onboarding_pipelines_ingest_report,
        "admin_onboarding_secrets": on_admin_onboarding_secrets,
        "admin_onboarding_validate": on_admin_onboarding_validate,
        "admin_onboarding_dbc_companies": on_admin_onboarding_dbc_companies,
        "admin_onboarding_qwc": on_admin_onboarding_qwc,
    }

    return rules, endpoints
