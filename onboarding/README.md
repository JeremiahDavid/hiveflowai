# HiveFlow connector onboarding

Operator-led client onboarding is available at **admin.hive-flow-ai.com → Onboarding**. Use the wizard to create `config.yaml` entries, store connector secrets, deploy CloudFormation stacks, and verify the handoff.

## Primary path (wizard)

1. Sign in to [platform admin](https://admin.hive-flow-ai.com/admin/login) as `GlobalAdmin`
2. Open **Onboarding → New client**
3. Complete client identity, connector, DNA/portal settings — config is written to `config.yaml`
4. On the client detail page:
   - **Save secret** — credentials go to AWS Secrets Manager (`hiveflow-{company}-{source}-{environment}`)
   - **Validate connector** — DBC smoke test, QBO OAuth status, or QBD secret check
   - **Deploy stacks** — triggers CodeBuild (`ProvisioningStack-{env}`) for `IngestStack` and `DnaStack` only. The reporting UI is the shared `PortalStack` and the client is reached via the `*.{zone}` wildcard — no per-client stack or DNS record. (`PortalStack-{env}` / `GlobalDnsStack-{env}` are deployed once per environment by the operator, not per onboard.)
5. Confirm stack status and post-deploy verification (governance seed, bronze manifest); note the **Client portal** URL on the deploy step
6. Optionally invite the first client portal admin on the **Deploy** step (after `DnaStack` completes), or skip — GlobalAdmin can sign in to the client portal and assign admins and users at `/portal/governance/users`

## Manual path (IDE/CLI)

Use this when the CodeBuild provisioner is not deployed or for debugging.

### Shared prerequisites (all connectors)

| Requirement | Notes |
|---|---|
| **AWS account** | Shared HiveFlow tenant account; CLI configured |
| **`config.yaml` entry** | `companies.{COMPANY}.environments.{ENV}` + matching `platform.environments.{ENV}.ui.portal.clients.{client_id}` |
| **Secrets** | AWS Secrets Manager `hiveflow-{company}-{source}-{environment}` via wizard or `python scripts/create_secrets.py --file secrets/...` |
| **CDK bootstrap** | One-time per account/region: `cdk bootstrap` |
| **Stack deploy** | `cdk deploy IngestStack-{COMPANY}-{ENV} DnaStack-{COMPANY}-{ENV}` (data plane per client). The shared `PortalStack-{ENV}` + `GlobalDnsStack-{ENV}` wildcard are deployed once per environment, not per client. |

Generic stack modules (`ingest_stack.py`, `dna_stack.py`) are shared — no per-company Python files.

### Connector guides

| Connector | Source key | Guide |
|---|---|---|
| QuickBooks Online | `qbo` | [quickbooks-online.md](./quickbooks-online.md) |
| QuickBooks Desktop (Web Connector) | `qbd` | [quickbooks-desktop.md](./quickbooks-desktop.md) |
| Dynamics 365 Business Central | `dbc` | [business-central.md](./business-central.md) |

**Admin UI:** On the client onboarding **Connector credentials** step, each connector has a **Credential setup guide** overlay with compact inline paste fields at each step (`<!-- credential-field:SECRET_KEY -->` markers embedded in list items). Field definitions and the **Where to find each input** table come from `CONNECTOR_CREDENTIAL_FIELDS` in `hiveflow.dna.web.admin.onboarding.guides`.

**New connectors:** add `onboarding/{connector-name}.md` with a marked credentials block and per-step `credential-field` markers, register the source key and form fields in `CONNECTOR_GUIDE_FILES` / `CONNECTOR_CREDENTIAL_FIELDS`, and copy the markdown file into `packages/hiveflow-portal/src/hiveflow/dna/web/admin/onboarding/guides/`.

### Provisioner (CodeBuild)

Deploy the provisioner once per platform environment:

```powershell
cdk deploy ProvisioningStack-dev
```

The admin **Deploy stacks** button calls `hiveflow-client-provision-{env}` with `HIVEFLOW_COMPANY`, `HIVEFLOW_ENVIRONMENT`, and `HIVEFLOW_PORTAL_CLIENT_ID` overrides.

## After onboarding

1. Confirm bronze data in S3: `s3://{bucket}/raw/{connector}/.../manifest.json`
2. Confirm silver consolidate ran (scheduled refresh or manual Step Functions execution)
3. Optional: run `hiveflow-sync-athena-catalog --source {connector}`
4. Complete the [pre-launch checklist](../docs/business-admin/pre-launch-checklist.md)

## Related docs

- [README — AWS deployment](../README.md)
- [Pre-launch checklist](../docs/business-admin/pre-launch-checklist.md)
- [Data lake architecture](../docs/internal-execution-scoping/data-lake-architecture.md)
- [HiveFlowAI domain setup](../docs/onboarding/hive-flow-ai-domain.md)
