# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**hiveflow** is the engineering monorepo for **HiveFlowAI**, a **DMaaS (Data Model as a Service)** platform: it exposes a governed, continuously updated semantic data model (dimensions, facts, relationships, metrics) through APIs so BI tools and AI agents can consume structured meaning without building the model themselves. Three capabilities: **Connect** (source ingest), **DNA Engine** (semantic modeling/governance), **Reporting Engine** (NL reports and portal).

**North star:** one centralized, governed data model across every system a business runs on, consumed by reporting and by AI agents that optimize operations. The target shape is **industry-specific connectors + industry-specific data model frameworks (packs)** layered on a shared universal core, so implementation for the next customer in an industry is fast. What exists today is the universal core plus a horizontal finance/ERP connector set — the vertical layers are direction, not built. Read [docs/product-vision.md](docs/product-vision.md) before making roadmap-shaped decisions (new connector, new pack, new engine surface); prefer changes that generalize into a pack over ones that solve a single customer.

First/reference connector: QuickBooks Online. Also supports QuickBooks Desktop (via Web Connector/SOAP) and Dynamics 365 Business Central (OData). Deploys to AWS via CDK — raw data lands in S3, ingest runs on scheduled Lambda/Step Functions/Glue.

Business/GTM/commercial docs live in the sibling repo `../hiveflow-business/` — never load that into engineering tasks.

## Setup

One shared virtualenv for the whole workspace, from repo root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
.\scripts\install_dev.ps1
```

This does an editable install of every package in `packages/` (platform → connectors → lake → dna → portal → hiveflow[dev]) plus AWS CDK libs and pytest. There is no per-package venv; always work from the repo-root `.venv`.

## Tests

Run from repo root (pytest is configured at the root `pyproject.toml` with `testpaths` covering `tests/` and every package's `tests/`):

```powershell
pytest
```

Scope to one package (after activating `.venv`):

```powershell
cd packages/hiveflow-connectors
pytest tests/test_spreadsheet_engine.py -v
pytest tests/test_spreadsheet_engine.py::test_name -v   # single test
```

Tests that touch AI (Bedrock, the KPI Generator, Spreadsheet Engine interpret/propose) pass `invoke=False` or otherwise stub the model call — no live Bedrock calls in unit tests.

There is no configured linter/formatter/type-checker (no ruff/black/mypy config in this repo) — don't assume one exists.

## CDK / deploy

```powershell
cdk bootstrap
cdk deploy IngestStack-POC-dev          # one target
cdk deploy --all                        # every non-prod target (prod excluded by default)
cdk deploy -c company=POC -c environment=dev
cdk deploy -c scope=platform GlobalUiStack-dev PortalStack-dev   # UI only — skips ingest/DNA, synths much faster
```

CDK entry is `infra/app.py`; scopes are `all` | `ingest` | `platform` (`HIVEFLOW_CDK_SCOPE` env or `-c scope=`). `prod` stacks are **not synthesized unless `HIVEFLOW_ENVIRONMENT=prod`** is explicitly set, and the active AWS account must match `config.yaml`'s configured `aws.account` for that environment — this is a deliberate guardrail against deploying prod resources into a dev account. See [README.md](README.md) for the full deploy walkthrough (secrets creation, OAuth, per-connector deploy commands).

**Multi-tenant portal.** Client reporting is **one** shared stack — `PortalStack-{env}` (single Lambda + API + `*.{zone}` wildcard domain) — not a stack per client. The tenant is resolved per request from the Cognito `custom:client_id` claim; the portal Lambda's own role can touch **no** tenant data and assumes `hiveflow-portal-tenant-{company}-{env}` (minted by each company's `DnaStack`) per request for S3/Athena/Glue/Step Functions. `config.yaml` (synced to `meshflow-platform-config-{env}` S3, re-hydrated per container) is the tenant registry. Onboarding a client = deploy its `IngestStack`+`DnaStack`, add the `platform.environments.<env>.ui.portal.clients.<id>` row, create Cognito users with `custom:client_id` — no per-client UI stack or DNS change. `HIVEFLOW_PORTAL_CLIENT_ID` is a local-dev / legacy single-tenant escape hatch only. `-c legacyReporting=true` re-synthesizes the retired per-client `ReportingStack`s + per-subdomain DNS for the cut-over/rollback window.

## Monorepo layout and package boundaries

This is a `packages/*` workspace where each package owns a layer of the data lake / product stack. **Prefer opening a single package folder as the editor workspace** to keep context small — each has its own `AGENTS.md` with a "default read set" and a "do not load" list; read the relevant one before working in that package.

| Package | Owns | Depends on |
|---|---|---|
| `hiveflow-platform` | Config (`project_config`, `process_config`), Secrets Manager, lake path layout (`hiveflow.storage.paths`), Parquet/JSON I/O (`hiveflow.storage.parquet`), entity registry, repo-root discovery | nothing else in-repo |
| `hiveflow-connectors` | Source connectors (`bc/`, `qbo/`, `qbd/`), ingest orchestration (`ingest/`), Spreadsheet Engine orchestration (`spreadsheet_lab/`) over shared substrate (`spreadsheet/`) | platform only |
| `hiveflow-lake` | Silver_stg consolidation (`silver/`), Glue/Athena catalog (`catalog/`) | platform only |
| `hiveflow-dna` | DNA semantic engine: compile/validate/publish/governance, packs, Athena SQL packs, source-docs scrape/gold pipeline | platform (not connectors, not portal) |
| `hiveflow-portal` | Portal UI, Cognito auth, charts, reporting surfaces (`src/hiveflow/dna/web/`), Spreadsheet Engine UI (`src/hiveflow/spreadsheet_lab/web/`, its own FastAPI app) | dna + platform |
| `hiveflow` | Thin CLI entrypoints (`cli.py`) wrapping the other packages | all of the above |

**Hard architectural rule: `hiveflow.dna` must never import `hiveflow.dna.web`.** Portal (`dna.web`, now physically in the `hiveflow-portal` package) depends on DNA; the dependency never goes the other direction. This is enforced by convention, not tooling, so watch for it when adding imports.

Other cross-package rules worth knowing:
- Connectors register entity resolvers via `hiveflow.entity_registry`; platform never imports connector-specific modules (`bc`/`qbo`/`qbd`/`silver`).
- Raw S3 key layout is defined once in platform's `storage.paths` — don't invent new key schemes in connectors or lake.
- BC MS Learn source documentation lives in `hiveflow-dna` (`hiveflow.dna.source_docs*`), even though BC ingest itself lives in connectors.

## Data lake layers (the core mental model)

```
raw/{qbo|qbd|dbc}/{run_id}/{entity}/data.parquet + manifest.json     # connector bronze landing
silver_stg/{source}/{entity}/data.parquet                            # ingest consolidate output — full catalog
silver/{source}/{entity}/data.parquet                                 # DNA-owned — pack entities only
gold/dna/{output_id}/data.parquet                                     # DNA-owned — facts/KPIs
governance/{company}_dna_config/v{semver}/...                         # pinned DNA config + SQL packs
```

- **Ingest consolidate** (hiveflow-lake) writes `silver_stg/` only — the full connector entity catalog, one Parquet file per entity, nested QBO/BC fields JSON-encoded as string columns for schema stability.
- **DNA Glue job** (`dna-apply`) copies only pack-referenced entities from `silver_stg` into `silver/`, replays pinned Athena SQL (silver column-adds, then gold facts/KPIs), and writes `gold/dna/*`. Approved SQL is pinned by governance semver (sha256'd in the manifest) and replayed **verbatim** on every scheduled refresh — Bedrock/AI is never called during a refresh, only when a human is drafting a new KPI.
- Silver SQL preserves entity grain (no `GROUP BY`/aggregates); grain-changing logic belongs in gold. At most one canonical silver transform (`enhance__{entity}`) per entity, one unique `grain_columns` set per gold output.
- Connector schedule (bronze + silver_stg) and DNA refresh (silver + gold) are separate Step Functions/EventBridge schedules (06:00 / 07:00 UTC in POC/dev) — see [docs/architecture.md](docs/architecture.md) for the full diagram and CDK stack table.

Two governed authoring flows build on this layered contract and are documented in depth — read the linked doc before working on either:
- **[KPI Generator](docs/kpi-generator.md)** (`hiveflow-portal`, `/portal/dna/kpi-generator`) — NL-driven drafting of silver/gold Athena SQL, with a draft → integrity-validate → approve → publish governance workflow.
- **[Spreadsheet Engine](docs/spreadsheet-engine.md)** — turns an uploaded `.xlsx` workbook into a governed `silver/reference/{entity}` entity via a Kanban-style review board (extract → clean shape → clean transform → done, plus a Discarded lane), with per-table drag-and-drop approval/discard and deterministic recipe replay (a file/table whose shape matches a prior approval skips straight to a to-confirm state with zero new AI calls). Orchestration module is `hiveflow.spreadsheet_lab` (`hiveflow-connectors`) — the name predates this being production, kept to avoid a package-rename with no functional benefit; the low-level substrate it imports (`hiveflow.spreadsheet.{_agent_runtime,synthesize,transform,materialize,parser}`) still lives in `hiveflow.spreadsheet`.
  - **Its own FastAPI app/Lambda**, not mounted into the Werkzeug `dna.web` portal — `packages/hiveflow-portal/src/hiveflow/spreadsheet_lab/web/`, deployed as part of `GlobalAgentPipelinesStack` (`infra/spreadsheet_engine.py`) at its own subdomain (`ui.spreadsheet_engine.hostname` in `config.yaml`, default `spreadsheet-engine`). It has no login of its own: `web/auth.py` validates the same signed `hiveflow_portal_session` cookie the real portal issues (`hiveflow.dna.web.portal.auth.session_from_request`, which duck-types across werkzeug/Starlette requests for exactly this), so `HIVEFLOW_PORTAL_COOKIE_DOMAIN` and `HIVEFLOW_PORTAL_SESSION_SECRET_ARN` must be set identically on both Lambdas.
  - **Multi-tenant like the rest of the portal**, not a standing per-company bucket grant: `web/tenant.py` resolves the session's `client_id` → `reporting_company` → bucket the same way `dna.web.portal.routes._portal_settings` does, then assumes that company's `hiveflow-portal-tenant-{company}-{env}` role for the request (`hiveflow.tenant_credentials`) — the shared Lambda execution role (`agent_pipelines_role_name(environment)`) is what every tenant role already trusts for this purpose. A per-table AI call runs too long for one HTTP request, so it self-invokes the same Lambda asynchronously (`worker.py`, same pattern as the KPI Generator's `enqueue_kpi_generation`) rather than using Step Functions; since that's a separate Lambda event with no ambient tenant context, the job doc carries `company` and the dispatching request's resolved bucket into the async payload, and the receiving invocation (`worker._worker_tenant_scope`) rebinds to that tenant itself before touching storage. The parquet write runs in a second, dedicated Lambda (`materialize_lab.py`) for the same reason production once needed it: pyarrow can't ship alongside pandas/pydantic/python-calamine without blowing Lambda's 250MB unzipped limit.

## Configuration

Deployment settings live in root **`config.yaml`** (gitignored secrets, not this file) supporting multiple companies × environments; see [README.md](README.md#configuration) for the full schema. No credentials are ever stored in `config.yaml` — OAuth tokens and app secrets live only in AWS Secrets Manager, created locally via `python scripts/create_secrets.py --file secrets/....yaml`. `secrets/` and `data/` are gitignored.

`HIVEFLOW_COMPANY` / `HIVEFLOW_ENVIRONMENT` env vars (or CDK `-c company=... -c environment=...`) override `config.yaml`'s `default:` block.
