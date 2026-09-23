# DNA Engine

Six governed DNA surfaces, one app: the catalog/pack browser, governance (compile/validate/publish/restore/versioning), Data Profile, Model Mapping, the Source Browser, and the [KPI Generator](./kpi-generator.md). Together these are the "DNA Engine" pillar of the product's three capabilities (Connect / **DNA Engine** / Reporting Engine — see [CLAUDE.md](../CLAUDE.md)).

It is a client-portal feature reachable from the portal shell's top nav (DNA / Agents / Governance tabs — see `dna_nav.py`), but served from its **own subdomain and Lambda**, the same pattern [Spreadsheet Engine](./spreadsheet-engine.md) established first — it trusts the portal's session cookie instead of having its own login.

**Site:** `https://dna-engine.{zone}/` (hostname from `config.yaml`'s `ui.dna_engine.hostname`)

**Backend (business logic, unchanged/reused from the former in-shell routes):** `packages/hiveflow-portal/src/hiveflow/dna/web/portal/{catalog.py, governance_helpers/, governance_restore.py, dna_manual_refresh.py, data_profile_ui/, model_mapping/, semantics/, kpi_generator/}` and the shared render functions in `portal/views.py` / `portal/semantics/source_docs_render.py`

**Web UI (routing seam):** `packages/hiveflow-portal/src/hiveflow/dna_engine/web/` (its own FastAPI app)

**Infrastructure:** `infra/dna_engine.py`, `infra/stacks/global_dna_engine_stack.py`

---

## Why its own app, not a page in the portal

Unlike Spreadsheet Engine, the driver here isn't a UI-interaction-model mismatch — it's bundle isolation and blast-radius separation. This content used to run inside the portal's single, multi-tenant `PortalStack` Lambda alongside the client-facing Reporting Engine dashboards: a bug or a slow Bedrock call in KPI Generator could degrade every client's reporting page, and every deploy of one touched the other. Splitting it out means:

- DNA Engine's Cognito admin actions (governance/users invites, role changes) and Bedrock access (KPI Generator drafting) live on a role scoped to this app only — `PortalStack`'s role no longer carries them.
- The two apps redeploy, scale, and roll back independently.
- `PortalStack` (now Reporting Engine-only) keeps its `*.{zone}` wildcard and login/session-issuance duties; DNA Engine is a single fixed hostname, the same shape as Spreadsheet Engine.

The routing/view seam is a thin FastAPI layer over the *same* business-logic modules and render functions the shell used to call directly — nothing here reimplements catalog browsing, governance, or KPI drafting; it reuses `hiveflow.dna.web.portal.views`' render functions unchanged via a small request shim (see `dna_engine/web/chrome.py`'s module docstring for why that's safe: those functions only ever read `request.script_root` off the request object they're given).

## Multi-tenancy

Like the rest of the multi-tenant portal, this app holds no standing per-company S3 grant — it resolves the tenant and assumes that company's role per request:

1. `dna_engine/web/tenant.py`'s `tenant_scope(client_id)` (entered by `app.py`'s auth middleware) resolves the session's `client_id` to a full `DnaSettings` + `ClientPortalConfig` via `hiveflow.dna.web.portal.tenant.resolve_tenant_dna_settings` and `portal.config.load_client_portal_config` — the same tenant-resolution helpers the shell's own strict/multi-tenant path and its KPI Generator async worker already used — then assumes `hiveflow-portal-tenant-{company}-{environment}` for the rest of the request (`hiveflow.tenant_credentials`).
2. The Lambda's own execution role (`hiveflow-dna-engine-{environment}-role`) is dedicated to this app — not shared with Spreadsheet Engine's `GlobalAgentPipelinesStack` role, since DNA Engine's permission surface (Cognito admin actions, Bedrock) is materially different.
3. KPI Generator's per-KPI Bedrock call runs longer than an HTTP request should — instead of Step Functions, the request writes a "working" proposal and asynchronously self-invokes the same Lambda (`enqueue_kpi_generation`, using `AWS_LAMBDA_FUNCTION_NAME` — the same self-invoke pattern Spreadsheet Engine borrowed *from* this feature, now running in DNA Engine's own Lambda instead of the shell's).
4. Admin-gating (governance writes, model-mapping writes, data-profile refreshes, KPI Generator actions) is computed once per request in the auth middleware (`is_portal_admin`, via `hiveflow.dna.web.portal.auth.require_portal_admin`) and stored on `request.state.is_admin`.

## Feature areas and route roots

| Surface | Route root | Notes |
|---|---|---|
| DNA landing | `/dna` | Redirects to the first catalog table |
| Catalog / pack browser | `/catalog`, `/catalog/gold`, `/catalog/silver[/{entity}]`, `/catalog/{output_id}` | |
| Governance | `/governance`, `/governance/users`, `/governance/config/preview/exit` | Compile/validate/publish is `POST /governance` action dispatch; restore/versioning via `governance_restore.py` |
| Data Profile | `/dna/data-profile`, `/dna/data-profile/{source}/refresh`, `/dna/data-profile/{source}/{entity}` | |
| Model Mapping | `/dna/model-mapping` | 9-action POST dispatch (init/approve/reject/exclude/add/promote) |
| Source Browser | `/semantics/source-docs[/{source}]`, `/api/source-docs-gold*` (8 JSON endpoints) | |
| [KPI Generator](./kpi-generator.md) | `/dna/kpi-generator`, `/dna/kpi-generator/status` | Nav label fixed from a prior "DNA Engine" collision — see `dna_nav.py` |

Route paths drop the shell's old `/portal` prefix — this is its own app on its own host now, same reasoning Spreadsheet Engine's routes use.

## Local development

```
uvicorn hiveflow.dna_engine.web.app:create_dna_engine_app --factory --reload
```

Every request still resolves a tenant via `config.yaml`'s client registry (`tenant.tenant_scope` isn't skipped locally), so a working `platform.environments.<env>.ui.portal.clients.<id>.reporting_company` entry is needed even off Lambda — only the STS assume-role call itself is skipped unless `HIVEFLOW_TENANT_ASSUME_ROLE=1` or actually running on Lambda, falling back to whatever the local AWS credential chain provides. Auth still requires a valid `hiveflow_portal_session` cookie from a locally-running portal shell, or a stubbed one signed with the same `HIVEFLOW_PORTAL_SESSION_SECRET`.

---

## Adding the next agent app

DNA Engine and Spreadsheet Engine are now two worked examples of the same shape. Building a third agent app the same way:

1. New FastAPI app under `hiveflow-portal`: `hiveflow.<name>.web.app::create_<name>_app()`.
2. Auth via the shared core only — import `hiveflow.dna.web.portal.auth.{session_from_request, require_portal_session_starlette, require_portal_admin}` directly, never reimplement session signing.
3. Theme/nav: hand-duplicate the small set of nav/theme constants the app needs if there's a real Lambda bundle-size conflict (Spreadsheet Engine's pattern, forced by pandas/pyarrow/python-calamine); import `hiveflow.dna.web.theme` / `portal.dna_nav` directly otherwise (DNA Engine's pattern — it has no such conflict, since it already needs the full `hiveflow-dna` package).
4. Tenant resolution: reuse `hiveflow.dna.web.portal.tenant.resolve_tenant_dna_settings` + `hiveflow.tenant_credentials.tenant_credentials` for a full `DnaSettings`, or a minimal bucket-only `tenant_scope` (Spreadsheet Engine's `tenant.py`) if the app only needs S3 access.
5. Own CDK module + stack (`infra/<name>.py` + `infra/stacks/global_<name>_stack.py`), importing the portal session secret **by name** (`Secret.from_secret_name_v2(portal_session_secret_name(env))`) and, if Cognito admin actions are needed, the user pool/client **by SSM parameter name** (`portal_user_pool_id_parameter_name`/`..._client_id_parameter_name`) — never a live `GlobalUiStack` construct reference, so the new scope can deploy in isolation. Give it its own `-c scope=<name>` entry in `infra/cdk_scope.py`/`infra/app.py` if its Lambda profile is heavy/independently-iterated (DNA Engine's pattern); fold it into `GlobalAgentPipelinesStack` only if it's as lightweight as Spreadsheet Engine.
6. Own config key: `ui.<name>.hostname` in `config.yaml`, resolved by a new `hiveflow.project_config.get_<name>_hostname(ui_config)`.
7. Own docs page (`docs/<name>.md`) following this file's Site/Backend/Web UI/Infrastructure/why-its-own-app/multi-tenancy/local-dev shape.
8. Update `CLAUDE.md`'s package table and the "future agents" section.
9. A `test_<name>_web.py` with, at minimum, the session-gating tests every app here has (no session → redirect/401, valid session → reaches the app, tampered cookie → rejected).
