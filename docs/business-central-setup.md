# Dynamics 365 Business Central — HiveFlow setup

This guide walks through connecting HiveFlow to **Business Central (BC)** using **service-to-service** auth (Azure app registration + client credentials). HiveFlow pulls OData entities on a schedule and lands Parquet under `raw/dbc/{run_id}/`.

**Mesh node:** `SYS-BC` · **Sample mesh:** `MESH-BC-INTRA`

---

## Overview

```text
Azure Entra ID app (client credentials)
      |
      v
BC Web Client: Microsoft Entra applications  (+ permission sets)
      |
      v
BC OData API  .../api/v2.0/companies({id})/...
      |
      v
HiveFlow ingest  -->  s3://.../raw/dbc/{run_id}/{entity}/data.parquet
      |
      v
Consolidate Glue  -->  silver_stg/dbc/{entity}/data.parquet
```

HiveFlow acquires and refreshes **`access_token`** automatically. You do **not** paste a token into the secrets file.

---

## Prerequisites

You need:

| Role / access | Used for |
|---------------|----------|
| **Global Administrator** or **Cloud Application Administrator** | Grant API permissions in Entra ID; **Grant Consent** in BC |
| **Dynamics 365 Administrator** (or BC admin) | BC Admin Center, enable app in **Microsoft Entra applications** |
| AWS access | Secrets Manager, optional CDK deploy |

> **Note:** Dynamics 365 Administrator alone is **not** enough to grant tenant-wide API consent in Entra ID.

---

## Step 1 — Register an app in Microsoft Entra ID

1. Open [Entra ID → App registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) → **New registration**.
2. Name: e.g. `HiveFlow BC Ingest`.
3. Supported account types: **Single tenant** (typical for one organization).
4. Register and copy:
   - **Application (client) ID** → `BC_CLIENT_ID`
   - **Directory (tenant) ID** → `BC_TENANT_ID`

### Redirect URI (required for BC Grant Consent)

HiveFlow ingest uses client credentials, but **Grant Consent** in Business Central still requires a **Web** redirect URI on the Entra app:

1. App registration → **Authentication** → **Add a platform** → **Web**.
2. **BC Online (SaaS):** `https://businesscentral.dynamics.com/OAuthLanding.htm`
3. **BC on-premises:** your web client URL + `/OAuthLanding.htm` (must match the browser address exactly).

Leave **Access tokens** and **ID tokens** (implicit grant) unchecked. HiveFlow ingest acquires application access tokens via the client-credentials flow at the token endpoint — it does not use implicit-grant or ID tokens through the redirect URI. The redirect is only for BC **Grant Consent** in the web client.

### Create a client secret

1. **Certificates & secrets** → **New client secret**.
2. Copy the **Value** immediately (shown once) → `BC_CLIENT_SECRET`.

> Use the secret **Value**, not the **Secret ID** (GUID). Using the Secret ID causes `AADSTS7000215: Invalid client secret`.

---

## Step 2 — API permissions (Entra ID)

1. App registration → **API permissions** → **Add a permission**.
2. **APIs my organization uses** → **Dynamics 365 Business Central**.
3. **Application permissions** (not Delegated) → **API.ReadWrite.All** (or **API.Read.All** for read-only).
4. **Grant admin consent for [your tenant]** — status must show a green check.

Without this step you may get a token from Azure but BC returns **401 Unauthorized**.

---

## Step 3 — Register the app in Business Central (required for data API)

This is the step that fixes most **401** errors on `/companies`.

> **Do not confuse** with **BC Admin Center → Authorized Microsoft Entra Apps**. That page is for the **Administration Center API** (environment management). HiveFlow reads **company data** via the standard OData API and needs registration **inside BC**.

1. Open **Business Central** for the target environment (e.g. Production).
2. Search (**Alt+Q**) for **Microsoft Entra applications**.
3. **New**:
   - **Client ID** — same as `BC_CLIENT_ID`
   - **Description** — e.g. `HiveFlow`
   - **State** — **Enabled**
4. Assign these permission sets on the app card (all required):
   - **ADD RELATED FIELDS**
   - **D365 AUTOMATION**
   - **D365 BUS FULL ACCESS**
5. Run **Grant Consent** on the app card and sign in as **Global Administrator** or **Cloud Application Administrator** if prompted.

Until this record exists and consent is granted, calls like:

`GET .../api/v2.0/companies`

will return **401**, even if the app appears in BC Admin Center.

---

## Step 4 — BC Admin Center (optional for HiveFlow ingest)

**BC Admin Center** → **Authorized Microsoft Entra Apps** authorizes apps for the **administration center API**, not for reading sales orders/invoices.

- Listing your app there is fine but **not sufficient** for HiveFlow ingest.
- The **Grant** link on that page may remain visible even after consent; that is a known UI quirk and does not block data API access once Step 3 is complete.

Use Admin Center to confirm your **environment name** (e.g. `Production`, `Sandbox`) → `BC_ENVIRONMENT_NAME`.

---

## Step 5 — Resolve IDs for the secrets file

| Secret key | What it is | Where to get it |
|------------|------------|-----------------|
| `BC_TENANT_ID` | Azure AD tenant | Entra app **Overview** → Directory (tenant) ID |
| `BC_CLIENT_ID` | App registration | Entra app **Overview** → Application (client) ID |
| `BC_CLIENT_SECRET` | App secret **Value** | Entra → Certificates & secrets (not Secret ID) |
| `BC_ENVIRONMENT_NAME` | BC environment slug | [BC Admin Center](https://businesscentral.dynamics.com/admin) — exact spelling/casing, e.g. `Production` |
| `BC_COMPANY_ID` | Company GUID in BC | Companies API (below) — **not** the same as tenant ID |
| `BC_ENVIRONMENT` | HiveFlow label | `sandbox` or `production` (metadata only) |

### List companies (get `BC_COMPANY_ID`)

After Steps 1–3, run in PowerShell:

```powershell
$tenant = "<BC_TENANT_ID>"
$clientId = "<BC_CLIENT_ID>"
$clientSecret = "<BC_CLIENT_SECRET>"
$bcEnv = "Production"   # must match Admin Center exactly

$token = (Invoke-RestMethod -Method Post `
  -Uri "https://login.microsoftonline.com/$tenant/oauth2/v2.0/token" `
  -Body @{
    grant_type    = "client_credentials"
    client_id     = $clientId
    client_secret = $clientSecret
    scope         = "https://api.businesscentral.dynamics.com/.default"
  }).access_token

Invoke-RestMethod `
  -Uri "https://api.businesscentral.dynamics.com/v2.0/$tenant/$bcEnv/api/v2.0/companies" `
  -Headers @{ Authorization = "Bearer $token" } |
  Select-Object -ExpandProperty value |
  Format-Table id, displayName
```

Copy the **`id`** for your company → `BC_COMPANY_ID`.

Leave token fields empty in YAML; HiveFlow fills them on first ingest:

```yaml
access_token: ""
expires_at: ""
```

Incremental watermarks are stored in S3 at `raw/dbc/_state/watermarks.json` (not in Secrets Manager).

---

## Step 6 — Secrets Manager

Copy `secrets.example.dbc.yaml` → `secrets/<company>-dbc-<environment>.yaml` and fill in values.

```powershell
python scripts/create_secrets.py --file secrets/poc-dbc-dev.yaml
# update later:
python scripts/create_secrets.py --file secrets/poc-dbc-dev.yaml --update
```

Secret name follows `config.yaml`: `hiveflow-{company}-{source}-{environment}` (e.g. `hiveflow-poc-dbc-dev`).

---

## Step 7 — config.yaml

Add a `dbc:` block under your company/environment:

```yaml
companies:
  POC:
    environments:
      dev:
        dbc:
          entity_bundle: full
          schedule:
            hour: 6
            minute: 0
```

### Entity bundles

| Bundle | Entities | Use |
|--------|----------|-----|
| **`full`** (default) | ~75 standard BC API v2.0 company entities — master data, sales/purchase docs (with lines), GL, inventory ledger, financial reports, workflows | Full operational lake for analytics / future MCP KPI queries |
| **`v1_intra`** | `customers`, `items`, `sales_orders`, `sales_shipments`, `sales_invoices`, `customer_payments` | `MESH-BC-INTRA` hero signals |
| **`v1_accounting`** | `customers`, `sales_invoices`, `open_sales_invoices`, `customer_payments` | Smaller accounting-focused pull |

Defined in [`packages/hiveflow-connectors/src/hiveflow/bc/entities.py`](../packages/hiveflow-connectors/src/hiveflow/bc/entities.py).

**Data model reference:** [dbc-data-model.md](./dbc-data-model.md) — entity relationships, join keys, and order-to-cash / procure-to-pay paths from Microsoft APV2 docs.

Ingest continues when individual entities fail (for example **403** when required BC permission sets are missing). Check `manifest.json` → `ingest_summary` and per-entity `status: failed` entries. Full ingest requires **ADD RELATED FIELDS**, **D365 AUTOMATION**, and **D365 BUS FULL ACCESS** on the BC Entra app card.

---

## Step 8 — Deploy (AWS)

Scheduled refresh uses **Step Functions**: bronze fan-out ingest (shared `run_id`, parallel Lambda per entity, manifest) then silver consolidate for that connector.

```powershell
cd infra
cdk deploy IngestStack-POC-dev
```

**DNA (optional, separate stack):** When `dna.enabled: true` in `config.yaml`, deploy the semantic engine independently after ingest:

```powershell
cdk deploy DnaStack-POC-dev
```

DNA runs on its own schedule (default 7:00 AM if ingest is 6:00 AM) — see [dna-semantic-engine.md](./internal-execution-scoping/dna-semantic-engine.md).

Stack outputs use the naming pattern `{company}-{environment}-{connector}-{stage}-{process}` (lowercase) unless a process sets `name_pattern`. Examples: `poc-dev-dbc-bronze-ingest`, connector refresh `poc-dev-dbc`, DNA Glue/SFN `poc-dev-dna`.

| Output | Example name |
|--------|----------------|
| `{CONNECTOR}BronzePrepareFunctionName` | `poc-dev-dbc-bronze-prepare` |
| `{CONNECTOR}BronzeIngestFunctionName` | `poc-dev-dbc-bronze-ingest` |
| `{CONNECTOR}BronzeFinalizeFunctionName` | `poc-dev-dbc-bronze-finalize` |
| `{CONNECTOR}RefreshStateMachineArn` | state machine `poc-dev-dbc` |
| `AllSilverConsolidateGlueJobName` | `poc-dev-silver-stg` |
| `DnaPublishFunctionName` (DnaStack) | `poc-dev-all-gold-dna-publish` |
| `DnaRefreshGlueJobName` (DnaStack) | `poc-dev-dna` |
| `DnaRefreshStateMachineArn` (DnaStack) | state machine `poc-dev-dna` |
| `QbdBronzeIngestFunctionName` | `poc-dev-qbd-bronze-ingest` |

Manual full refresh (bronze + silver):

```powershell
aws stepfunctions start-execution `
  --state-machine-arn <DbcRefreshStateMachineArn> `
  --input '{\"full_load\": false, \"full_rebuild\": false}'
```

Manual full reload (ignore incremental watermarks and rebuild silver):

```powershell
aws stepfunctions start-execution `
  --state-machine-arn <DbcRefreshStateMachineArn> `
  --input '{\"full_load\": true, \"full_rebuild\": true}'
```

Silver consolidate only:

```powershell
aws glue start-job-run `
  --job-name poc-dev-silver-stg `
  --arguments='{\"--HIVEFLOW_SOURCE\":\"dbc\",\"--full_rebuild\":\"false\"}' `
  --region us-east-2
```

Single entity (ad-hoc bronze ingest for one entity):

```powershell
aws lambda invoke `
  --function-name poc-dev-dbc-bronze-ingest `
  --payload '{\"entity\": \"customers\"}' `
  out.json
```

> **QuickBooks Desktop (QBD)** is not fan-out scheduled ingest — Web Connector pulls entities sequentially in one QBWC session by platform design.

With a `dbc:` block, CDK provisions the **DBC refresh pipeline** (bronze fan-out + silver_stg consolidate), entity ingest, Glue/Athena tables `raw_dbc_*` / `silver_stg_dbc_*` (ingest) and `silver_dbc_*` / `dna_*` (DNA), and reuses the shared data bucket.

---

## Step 9 — Run ingest

**Local / manual:**

```powershell
$env:HIVEFLOW_COMPANY = "POC"
$env:HIVEFLOW_ENVIRONMENT = "dev"
$env:HIVEFLOW_SOURCE = "dbc"
$env:HIVEFLOW_SECRET_ID = "hiveflow-poc-dbc-dev"

python scripts/bc_ingest.py
```

**Full reload** (ignore incremental watermarks):

```powershell
python scripts/bc_ingest.py --full-load
```

**Single entity:**

```powershell
python scripts/bc_ingest.py --entity customers
```

**Consolidate to silver:**

```powershell
python scripts/consolidate.py --source dbc
```

Incremental watermarks (`lastModifiedDateTime` per entity) are stored in S3 at `raw/dbc/_state/watermarks.json` after each successful run.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `AADSTS7000215: Invalid client secret` | Secret ID used instead of secret Value | Create new secret; copy **Value** |
| **401** on `/companies` | App not in **BC → Microsoft Entra applications**, or missing Entra admin consent | Complete Steps 2 and 3 |
| **401** after Admin Center only | Wrong authorization path | Admin Center ≠ data API; do Step 3 |
| Admin Center **Grant** still shows link | UI quirk for admin API | Ignore if Step 3 works and API returns companies |
| **404** on API URL | Wrong `BC_ENVIRONMENT_NAME` | Match Admin Center exactly (`Production` vs `Sandbox`) |
| Empty companies list | App lacks permission sets in BC | Assign **ADD RELATED FIELDS**, **D365 AUTOMATION**, and **D365 BUS FULL ACCESS** on the BC Entra app card |
| Entity reads **403** during ingest or validation | Missing BC permission sets on Entra app card | Assign all required sets above, **Grant Consent**, then re-run **Validate connector** |
| Consent appears to do nothing | Signed-in user lacks Entra admin role | Use Global Admin or Cloud Application Administrator |

---

## Security notes

- Rotate client secrets if exposed in logs or chat.
- Do not commit `secrets/*.yaml` with real credentials.
- Prefer least-privilege permission sets in BC for production; the **full** entity bundle requires **ADD RELATED FIELDS**, **D365 AUTOMATION**, and **D365 BUS FULL ACCESS** on the BC Entra app card.

---

## Related docs

- [Data lake architecture](./internal-execution-scoping/data-lake-architecture.md) — BC ingest pattern
- [DBC data model](./dbc-data-model.md) — entity relationships and join paths
- [Product vision](./product-vision.md) — BC is a **T1 direct, horizontal** connector; see connector tiers
- Node / mesh catalogs (`SYS-BC`, `MESH-BC-INTRA`) — moved to the sibling business repo, under the pre-rename `mesh-*` names
