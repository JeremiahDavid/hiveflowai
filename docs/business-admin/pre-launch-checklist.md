# Pre-launch checklist

Complete before handing a new client portal to end users.

## Configuration

- [ ] `companies.{COMPANY}.environments.{ENV}` exists in `config.yaml`
- [ ] Matching `platform.environments.{ENV}.ui.portal.clients.{client_id}` with `reporting_company` set
- [ ] Connector secret exists in Secrets Manager (`hiveflow-{company}-{source}-{environment}`)
- [ ] Connector validation passed (DBC OData, QBO OAuth tokens, or QBD SOAP URL + `.qwc` installed)

## Infrastructure (CloudFormation)

- [ ] `IngestStack-{COMPANY}-{ENV}` — `CREATE_COMPLETE`
- [ ] `DnaStack-{COMPANY}-{ENV}` — `CREATE_COMPLETE` (when DNA enabled) — also mints `hiveflow-portal-tenant-{company}-{ENV}`
- [ ] Client row synced to `config.yaml` in `meshflow-platform-config-{ENV}` S3 (`platform.environments.{ENV}.ui.portal.clients.{client_id}` with `reporting_company`)
- [ ] `PortalStack-{ENV}` + `GlobalDnsStack-{ENV}` already deployed for the environment (once, not per client)
- [ ] `{client_id}.hive-flow-ai.com` resolves via the `*.{zone}` wildcard and serves the portal login
- [ ] At least one portal admin invited (`custom:portal_role=admin`) — the role default is now `member`

## Data plane

- [ ] S3 data bucket created (`hiveflow-{company}-{account}-{region}`)
- [ ] Bronze manifest present under `raw/{source}/`
- [ ] Silver consolidate completed at least once (`silver_stg/{source}/`)
- [ ] Governance packs seeded (`governance/{company}_dna_config/workflow.json`)
- [ ] DNA refresh completed at least once (gold outputs under `gold/dna/`)

## Portal

- [ ] Portal login works at client subdomain
- [ ] At least one portal admin user invited (`/portal/governance/users`)
- [ ] Executive / catalog pages render from `{company}_reporting_config`
- [ ] Manual refresh quotas configured (`dna_manual_refresh`, `silver_manual_refresh`)

## Handoff

- [ ] Document refresh cadence and named client contacts in SOW
- [ ] Share portal URL and admin invite instructions with client
- [ ] Record onboarding date and connector source in internal CRM/tracker
