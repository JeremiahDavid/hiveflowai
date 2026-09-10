# Platform Architecture (Current State)

How HiveFlowAI / hiveflow is deployed today: AWS stacks, domains, connectors, and user-facing surfaces.

**Audience:** Internal product and engineering. Not customer-facing.

**Scope:** Reflects the **dev / POC** deployment driven by `config.yaml` and CDK stacks under `infra/`. Product brand is **HiveFlowAI**; repo/package is **hiveflow**.

## Product framing — DMaaS

**North star:** one centralized, governed data model spanning every system a business runs on, consumed by reporting and by AI agents that optimize operations. The target shape is **industry-specific connectors feeding industry-specific data model frameworks** on top of a shared universal core. Full framing, connector tiers, and vertical prioritization: [product-vision.md](./product-vision.md).

**DMaaS (Data Model as a Service)** is the delivery mechanism for that model — a cloud service that exposes the fully built, governed, continuously updated semantic data model (dimensions, facts, relationships, metrics) through APIs so applications, BI tools, and AI agents can consume structured meaning without building the model themselves.

Inside DMaaS:

| Capability | Role |
|---|---|
| **Connect** | Land source systems into the lake. Today: Business Central, QuickBooks Online, QuickBooks Desktop, and `.xlsx` upload — a **horizontal finance/ERP foundation**, not yet an industry connector set |
| **DNA Engine** | Process owners tailor the semantic model in plain language; approved logic is pinned. The mechanism that industry framework packs will be built on |
| **Reporting Engine** | Natural-language reports and portal layouts bound to certified DNA metrics |

Scheduled refresh updates **data** inside the model. Semantic and layout code change only when DNA or reporting packs are promoted.

**Current state vs. target:** everything documented below is the **universal core** — source- and industry-agnostic. The industry connector and industry framework pack layers described in [product-vision.md](./product-vision.md) are direction, not deployed. Nothing on this page is scheduled to change because of them.

**Companion docs:**

- [product-vision.md](./product-vision.md) — north star, layer model, connector tiers, vertical prioritization
- [data-lake-architecture.md](./internal-execution-scoping/data-lake-architecture.md) — bronze / silver / gold lake design
- [dna-semantic-engine.md](./internal-execution-scoping/dna-semantic-engine.md) — DNA packs → gold → portal
- [bc-source-documentation-lambdas.md](./bc-source-documentation-lambdas.md) — BC MS Learn source-docs scrape / relationships / tags
- [hive-flow-ai-domain.md](./onboarding/hive-flow-ai-domain.md) — Squarespace ↔ Route 53 ↔ UI/DNS stack split
- [pre-launch-checklist.md](./business-admin/pre-launch-checklist.md) — infra provisioning checklist

---

## System diagram

```mermaid
flowchart TB
  %% ========== USERS & EXTERNAL ==========
  subgraph Users["Users"]
    Visitor["Public visitors"]
    PortalUser["Portal users"]
    Admin["Portal admins"]
  end

  subgraph External["External systems"]
    SQ["Squarespace<br/>domain registrar"]
    QBO["Intuit QuickBooks Online<br/>OAuth2 API"]
    QBD["QuickBooks Desktop<br/>+ Web Connector QBWC"]
    Entra["Microsoft Entra ID"]
    BC["Dynamics 365<br/>Business Central OData"]
  end

  %% ========== DNS / EDGE ==========
  subgraph DNS["GlobalDnsStack-dev · us-east-2"]
    R53["Route 53 hosted zone<br/>hive-flow-ai.com<br/>Z0833907O664KG7NO3CQ"]
    ACM["ACM certificates<br/>apex · www · poc"]
    CD_APEX["API GW custom domain<br/>hive-flow-ai.com / www"]
    CD_POC["API GW custom domain<br/>*.hive-flow-ai.com (wildcard)"]
  end

  SQ -->|"NS delegated to Route 53"| R53
  R53 --> ACM
  ACM --> CD_APEX
  ACM --> CD_POC

  %% ========== PLATFORM UI ==========
  subgraph Platform["Platform UI plane"]
    subgraph GlobalUI["GlobalUiStack-dev"]
      GAPI["API Gateway REST<br/>hiveflow-global-ui"]
      GLAM["Lambda<br/>platform-dev-global-ui-serve<br/>HIVEFLOW_UI_MODE=global"]
      COG["Cognito User Pool<br/>attrs: client_id, portal_role"]
      SES["SES domain identity<br/>noreply@hive-flow-ai.com"]
      SEC["Secrets Manager<br/>portal session secret"]
    end

    subgraph Reporting["PortalStack-dev (one, multi-tenant)"]
      RAPI["API Gateway REST<br/>hiveflow-portal-dev"]
      RLAM["Lambda<br/>portal-dev-reporting-ui-serve<br/>HIVEFLOW_UI_MODE=reporting_multitenant<br/>tenant = Cognito client_id claim<br/>assumes hiveflow-portal-tenant-{company}-dev per request"]
    end
  end

  Visitor -->|"https://hive-flow-ai.com<br/>/ · /platform · /pricing"| CD_APEX
  PortalUser -->|"https://hive-flow-ai.com/portal/login"| CD_APEX
  Admin -->|"portal admin users"| CD_APEX
  PortalUser -->|"https://&lt;client&gt;.hive-flow-ai.com<br/>executive · revenue · charts"| CD_POC

  CD_APEX --> GAPI --> GLAM
  CD_POC --> RAPI --> RLAM

  GLAM --> COG
  GLAM --> SES
  GLAM --> SEC
  RLAM --> COG
  RLAM --> SEC
  GlobalUI -.->|"exports Cognito + session"| Reporting

  %% ========== COMPANY DATA PLANE ==========
  subgraph Company["Company data plane · POC / dev"]
    subgraph Ingest["IngestStack-POC-dev"]
      SM_QBO["Secrets Manager<br/>hiveflow-poc-qbo-dev"]
      SM_DBC["Secrets Manager<br/>hiveflow-poc-dbc-dev"]
      SM_QBD["Secrets Manager<br/>hiveflow-poc-qbd-dev"]

      EB_ING["EventBridge schedules<br/>06:00 UTC QBO/DBC"]
      SF_QBO["Step Functions<br/>poc-dev-qbo"]
      SF_DBC["Step Functions<br/>poc-dev-dbc"]
      SF_QBD["Step Functions<br/>poc-dev-qbd"]

      L_PREP["Lambdas · prepare / entity ingest / finalize"]
      SOAP_API["API Gateway REST<br/>QBD SOAP /soap"]
      L_SOAP["Lambda · QBD SOAP handler"]
      L_SILVER["Glue · silver-consolidate"]

      LAKE["S3 data lake<br/>hiveflow-poc-{account}-us-east-2<br/>raw → silver_stg → silver → gold"]
      GLUE["Glue Data Catalog<br/>hiveflow_poc_dev<br/>raw_* · silver_stg_* · silver_* · dna_*"]
      ATH["Athena workgroup<br/>hiveflow-poc-dev"]
      ATH_S3["S3 Athena results<br/>athena-results-poc-…<br/>30-day lifecycle"]
    end

    subgraph DNA["DnaStack-POC-dev"]
      EB_DNA["EventBridge<br/>poc-dev-dna · 07:00 UTC"]
      SF_DNA["Step Functions<br/>poc-dev-dna"]
      G_DNA["Glue · dna-apply<br/>silver_stg → silver + gold"]
    end
  end

  %% ========== CONNECTOR FLOWS ==========
  QBO -->|"OAuth2 pull"| SF_QBO
  Entra --> BC
  BC -->|"client-credentials OData"| SF_DBC
  QBD -->|"QBWC SOAP poll"| SOAP_API --> L_SOAP --> LAKE

  EB_ING --> SF_QBO & SF_DBC & SF_QBD
  SF_QBO & SF_DBC --> L_PREP --> LAKE
  SF_QBO & SF_DBC & SF_QBD --> L_SILVER --> LAKE
  SM_QBO --> SF_QBO
  SM_DBC --> SF_DBC
  SM_QBD --> L_SOAP

  EB_DNA --> SF_DNA --> G_DNA -->|"silver/* · gold/dna/*"| LAKE
  LAKE --> GLUE --> ATH --> ATH_S3

  RLAM -->|"read gold Parquet / JSON"| LAKE

  %% ========== SURFACES LEGEND ==========
  subgraph Surfaces["User-facing surfaces"]
    Site["Marketing site<br/>hive-flow-ai.com · www"]
    Login["Portal login / admin<br/>/portal/login"]
    Dash["Client reporting dashboard<br/>&lt;client&gt;.hive-flow-ai.com"]
  end

  Site -.-> GAPI
  Login -.-> GAPI
  Dash -.-> RAPI
```

---

## CDK stacks

| Stack | Module | Role |
|---|---|---|
| **IngestStack-POC-dev** | `infra/stacks/ingest_stack.py` | Data lake S3, connector Lambdas / Step Functions / EventBridge, QBD SOAP API, Glue, Athena |
| **GlobalDnaStack-dev** | `infra/stacks/global_dna_stack.py` | Global BC MS Learn source-docs scrape / relationships / tags |
| **DnaStack-POC-dev** | `infra/stacks/dna_stack.py` | DNA publish + per-client source-docs gold merge; mints `hiveflow-portal-tenant-{company}-{env}` (the role PortalStack assumes for this company's data) |
| **GlobalUiStack-dev** | `infra/stacks/global_ui_stack.py` | Public site, Cognito, SES, session secret |
| **PortalStack-dev** | `infra/stacks/portal_stack.py` | **One** multi-tenant client reporting Lambda + API for every client (`HIVEFLOW_UI_MODE=reporting_multitenant`); tenant from the Cognito `client_id` claim; assumes a per-company tenant role for all data access; canary alias deploy. Replaces per-client `ReportingStack` (retired; `-c legacyReporting=true` to re-synth during cut-over) |
| **GlobalDnsStack-dev** | `infra/stacks/global_dns_stack.py` | Route 53, ACM, API Gateway custom domains — apex/`www`/`admin.` + the `*.{zone}` wildcard that routes every client subdomain to PortalStack (when `manage_dns: true`) |

CDK entry: `infra/app.py`. Scopes: `all` | `ingest` | `platform` (`HIVEFLOW_CDK_SCOPE` / `-c scope=`).

**Not in current design:** CloudFront, DynamoDB, SQS, SNS, Kinesis.

---

## User-facing surfaces

| Surface | URL | Stack |
|---|---|---|
| Marketing / public site | `https://hive-flow-ai.com/`, `www` | GlobalUiStack |
| Portal login / admin | `https://hive-flow-ai.com/portal/login` | GlobalUiStack |
| Client reporting dashboard | `https://<client>.hive-flow-ai.com/` (`*.{zone}` wildcard) | PortalStack |
| QBD SOAP (ops) | stack output `QbdSoapUrl` (`…/prod/soap`) | IngestStack |

App code: `packages/hiveflow-portal/packages/hiveflow-portal/src/hiveflow/dna/web/` (Werkzeug WSGI → `aws-wsgi` on Lambda).

---

## External systems

| System | Relationship |
|---|---|
| **Squarespace** | Registrar for `hive-flow-ai.com`. NS delegated to Route 53; does not host the live site once delegated. |
| **Intuit QuickBooks Online** | OAuth2 → bronze ingest (`qbo`) |
| **QuickBooks Desktop + QBWC** | SOAP poll → QBD Lambda via API Gateway `/soap` |
| **Microsoft Entra ID + Business Central** | Client-credentials OData → bronze ingest (`dbc`) |

---

## Data flow

### Lake layout (company bucket)

```text
s3://hiveflow-{company}-{account}-{region}/
  raw/{qbo|qbd|dbc}/{run_id}/{entity}/data.parquet + manifest
  silver_stg/{source}/{entity}/data.parquet
  silver/{source}/{entity}/data.parquet
  gold/dna/_staging/...
  gold/dna/{output_id}/data.parquet
  governance/{company}_dna_config/workflow.json
  governance/{company}_dna_config/v{semver}/{company}_dna_config.yaml
  governance/{company}_dna_config/v{semver}/{company}_reporting_config.yaml
  governance/{company}_dna_config/v{semver}/sql/manifest.yaml
  governance/{company}_dna_config/v{semver}/sql/silver/*.sql
  governance/{company}_dna_config/v{semver}/sql/gold/*.sql
  governance/{company}_dna_config/v{semver}/docs/...
  governance/{company}_dna_config/v{semver}/manifest.json
```

**Layer contract (KPI Generator / Athena SQL packs):**

| Change | Layer | Runs when |
|---|---|---|
| Derived **column** adds on an existing entity | **silver** (`sql/silver/*`) | DNA refresh (07:00); reads `silver_stg_*`, writes `silver/` |
| New **fact/dim/cube** tables and **KPIs** | **gold** (`sql/gold/*`) | DNA refresh (07:00); reads DNA `silver_*` |

Approved SQL is pinned by governance semver and replayed **verbatim** on schedule (no AI on refresh). Portal workflow: generate → save draft → review → approve. See [kpi-generator.md](./kpi-generator.md).

**Cutover (existing lakes):** run `python scripts/copy_silver_to_silver_stg.py` to copy `silver/` → `silver_stg/` (does not delete `silver/`). Deploy IngestStack, then DnaStack, then run DNA refresh once so DNA `silver/` and `gold/dna/` are rewritten.

### Connector refresh (IngestStack)

```text
EventBridge cron
  → Step Functions {company}-{env}-{connector}
      → [QBO/DBC] prepare → Map(entity ingest) → finalize
      → [QBD] skip bronze (QBWC already wrote raw)
      → silver consolidate Glue job (writes silver_stg only)
```

### DNA refresh (DnaStack)

```text
EventBridge {company}-{env}-dna
  → Step Functions DNA refresh
      → Glue dna-apply (2h)
           · copy pack-referenced silver_stg entities → silver (SQL targets + gold sources)
           · drop unused silver/ prefixes and Glue silver_* tables
           · pinned Athena silver SQL (column adds from silver_stg_*)
           · if pinned sql/gold present: Athena gold materialize
           · else: legacy Python compile → validate → publish
           · write gold/dna/manifest.json (pack_version + silver_sql_pack_version)
  → writes silver/{pack entities} and gold/dna/* ; Glue catalog updates
```

### UI read path

```text
Browser → Route 53 → API Gateway custom domain → Lambda
  Global: public pages + Cognito login/admin
  Reporting: Cognito session cookie → read gold from company S3
```

---

## Config anchors (`config.yaml`)

| Key | Value (dev) |
|---|---|
| Region | `us-east-2` |
| Zone / primary | `hive-flow-ai.com` |
| Hosted zone ID | `Z0833907O664KG7NO3CQ` |
| Portal clients | `poc`, `poc2` → `<client>.hive-flow-ai.com` via the `*.{zone}` wildcard → PortalStack |
| DNA source / pack | `dbc` / `{company}_dna_config` + `{company}_reporting_config` |
| Connector schedules | QBO/DBC 06:00 UTC; DNA 07:00 UTC |
| SES from | `noreply@hive-flow-ai.com` |

Routine UI deploys keep `manage_dns: false`. Deploy **GlobalDnsStack** only for DNS bootstrap or recovery — see [hive-flow-ai-domain.md](./onboarding/hive-flow-ai-domain.md).
