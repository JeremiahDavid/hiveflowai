# Spreadsheet Engine

Turns an uploaded Excel workbook (`.xlsx`) into a governed reference entity: detect tables, propose schema/cleaning with Bedrock, let an operator review and approve each step on a Kanban-style board, and materialize approved tables into `silver/reference/{entity}` parquet.

It is a client-portal feature reachable from the Source Browser's nav (see `dna_nav.py`), but served from its **own subdomain and Lambda** rather than being mounted into the Werkzeug portal app — it trusts the portal's session cookie instead of having its own login.

**Site:** `https://spreadsheet-engine.{zone}/` (hostname from `config.yaml`'s `ui.spreadsheet_engine.hostname`)

**Backend (orchestration):** `packages/hiveflow-connectors/src/hiveflow/spreadsheet_lab/`

**Backend (shared substrate, reused by import):** `packages/hiveflow-connectors/src/hiveflow/spreadsheet/` (`_agent_runtime.py`, `synthesize.py`, `transform.py`, `materialize.py`, `parser.py`)

**Web UI:** `packages/hiveflow-portal/src/hiveflow/spreadsheet_lab/web/` (its own FastAPI app, Jinja2 templates, no shared code with `dna.web`)

**Infrastructure:** `infra/spreadsheet_engine.py`, wired into `infra/stacks/global_agent_pipelines_stack.py`

---

## Why its own app, not a page in the portal

The board's UI needs a real modal dialog and native drag-and-drop, neither of which exist anywhere else in the portal (which uses `<details>/<summary>` for disclosure and has no JS drag-and-drop at all). Building it as a small, independent FastAPI/Jinja app was simpler and lower-risk than either porting that interaction model into the portal's raw-HTML-string `render.py` style or bridging an ASGI app into the Werkzeug app in-process. It still needs to behave like a portal feature, so it shares two things with the real portal rather than reinventing them:

- **Auth**: `web/auth.py` validates the same signed `hiveflow_portal_session` cookie the portal issues, via `hiveflow.dna.web.portal.auth.session_from_request` (which duck-types across werkzeug and Starlette `Request` objects for exactly this). No valid session → redirect to the portal's own `/portal/login` with `next` pointing back at this app's (absolute, cross-subdomain) URL. This only works if both Lambdas are configured with the same `HIVEFLOW_PORTAL_COOKIE_DOMAIN` (e.g. `.hive-flow-ai.com`, so the cookie reaches both hosts) and the same `HIVEFLOW_PORTAL_SESSION_SECRET_ARN` (so both sign/verify with the identical secret).
- **Theme**: `/static/theme.css` and the brand SVGs are served straight from `hiveflow.dna.web`'s static directory (`app.py`'s `_DNA_STATIC_ASSETS` whitelist) rather than duplicated, so the two can't drift out of sync. `layout.html` reproduces `_layout.html`'s topbar/brand/footer chrome; the board itself uses new `se-*`-prefixed classes appended to the end of `theme.css` for the modal and drag-and-drop states that don't exist elsewhere.

## Multi-tenancy

Like the rest of the multi-tenant portal, this app holds no standing per-company S3 grant — it resolves the tenant and assumes that company's role per request:

1. `web/tenant.py`'s `tenant_scope(client_id)` (entered by `app.py`'s auth middleware) resolves the session's `client_id` → `reporting_company` → data bucket, exactly like `dna.web.portal.routes._portal_settings`'s strict branch, then assumes `hiveflow-portal-tenant-{company}-{environment}` for the rest of the request (`hiveflow.tenant_credentials`).
2. The Lambda's own execution role is the same shared `agent_pipelines_role_name(environment)` role every tenant role already trusts for this purpose (no per-app IAM wiring needed).
3. A per-table AI call runs longer than an HTTP request should — instead of Step Functions, the request writes a `"processing"` stub and asynchronously self-invokes the same Lambda (`worker.py`, the same pattern as the portal's KPI Generator). That second invocation is a genuinely separate Lambda event with no ambient tenant context, so the job document's `company` field and the enqueuing request's already-resolved bucket are carried into the invocation payload, and `worker._worker_tenant_scope` rebinds to that tenant before touching storage.
4. The parquet write runs in a *third*, dedicated Lambda (`materialize_lab.py`) for the same reason production's old pipeline needed one: `pyarrow` can't ship in the same package as `pandas`/`pydantic`/`python-calamine` without exceeding Lambda's 250MB unzipped limit. It receives `{job_id, table_id, company, bucket}` and rebinds the same way.

## Storage layout

```
governance/spreadsheet_engine/jobs/{job_id}/job.json
governance/spreadsheet_engine/jobs/{job_id}/upload/{filename}
governance/spreadsheet_engine/jobs/{job_id}/parse.json
governance/spreadsheet_engine/jobs/{job_id}/tables/{table_id}.json
governance/spreadsheet_engine/recipes/files/{file_shape_hash}.json
governance/spreadsheet_engine/recipes/tables/{table_shape_hash}.json
silver/reference/{entity}/data.parquet
```

All under the **tenant's own bucket** (resolved per request, see above) — same key scheme every reporting company gets, not a shared/pooled location.

## The review board

Each table moves through a fixed set of lanes, computed server-side from `(phase, status, clean_shape_status)` (`app.py::_lane_for_table`):

1. **Extracting** — the deterministic heuristic parser (falling back to a Bedrock "blind scan" via `list_sheets`/`get_sheet_map`/`read_range` if it finds nothing) proposes a region + schema; the operator approves, discards, or rejects with feedback (which re-invokes the same agent with the prior proposal + feedback attached — no persisted transcript, same pattern as the KPI Generator).
2. **Approved — awaiting cleaning** → **Cleaning: shape** → **Cleaning: transform** — the AI proposes a cleaned shape (`clean_goal`), then deterministic transform steps; each is independently approvable, rejectable-with-feedback, or discardable.
3. **Done** — materialized to `silver/reference/{entity}`, with a preview of the actual output rows and any cast-safety notes (e.g. a column with mixed types got coerced to text rather than dropping rows).
4. **Discarded** — reachable from any lane (a per-card Discard button, or dragging a card onto this lane); tables here are excluded from the finished-job check but remembered on re-upload (see recipes).

The board supports a bulk **"Approve all"** per lane (fans out to the same single-table approve call the card's own button would use) and **drag-and-drop**: dropping a card on the Discarded lane discards it; dropping it on any lane strictly to the right of its current one approves it; anything else is a silent no-op.

## Deterministic recipe replay

Once every table in a file is terminal (approved or discarded), `file_recipe.compile_file_recipe` keys the outcome (region + schema per table, or "discarded") to a whole-file shape signature. A later upload whose shape matches replays the recipe with **zero new AI calls** — but always lands in a to-confirm review state (`pending_review`/`pending_transform_review`), never auto-approved; a human still confirms before materialization. Likewise, `table_recipe.py` keys a table's *transformation* to its own input-shape hash, independent of which file it came from, so a table with a familiar shape in a brand-new file skips phase 2's AI call too. A phase-2 discard (which happens after the file recipe was already compiled at phase-1 completion) is captured by recompiling the file recipe when the job actually finishes, so it's remembered on the next re-upload as well. An explicit "Force re-run" button ignores a matched recipe and goes through full review again.

## Local development

```
uvicorn hiveflow.spreadsheet_lab.web.app:create_spreadsheet_lab_app --factory --reload
```

Every request still resolves a tenant bucket from `config.yaml`'s client registry (`tenant.tenant_scope` isn't skipped locally), so a working `platform.environments.<env>.ui.portal.clients.<id>.reporting_company` entry is needed even off Lambda — only the STS assume-role call itself is skipped (`tenant_credentials` is a no-op unless `HIVEFLOW_TENANT_ASSUME_ROLE=1` or actually running on Lambda), falling back to whatever the local AWS credential chain provides. Auth still requires a valid `hiveflow_portal_session` cookie from a locally-running portal, or a stubbed one signed with the same `HIVEFLOW_PORTAL_SESSION_SECRET`.
