# HiveFlowAI custom domain (hive-flow-ai.com)

DNS (Route 53 records, ACM certificates, API Gateway custom domains) lives in **`GlobalDnsStack-{environment}`**, not in `GlobalUiStack`. That way routine website deploys never touch hosted zones.

**Routine deploys:**

```powershell
cdk deploy GlobalUiStack-dev
```

With steady-state config (`manage_dns: false`), only Lambda, API Gateway, and Cognito are updated. Deploy **GlobalDnsStack** separately when DNS changes — not on every UI deploy.

If GlobalUi deploy fails with *Cannot delete export ... in use by GlobalDnsStack*, run this **three-step migration** (custom domain is briefly unavailable between steps 1 and 3):

```powershell
# 1. Remove old base path mappings and release the cross-stack export lock
cdk deploy GlobalDnsStack-dev -c scope=platform --exclusively -c dnsManageBasePathMappings=false

# 2. Deploy the UI Lambda fix and refresh API exports on both stacks
cdk deploy GlobalUiStack-dev PortalStack-dev -c scope=platform --exclusively

# 3. Recreate base path mappings (uses stack exports from step 2)
cdk deploy GlobalDnsStack-dev -c scope=platform --exclusively
```

If step 3 fails with *No export named hiveflow-...-web-api-id*, either rerun step 2 first, or pass API IDs explicitly:

```powershell
cdk deploy GlobalDnsStack-dev -c scope=platform --exclusively `
  -c globalWebApiId=jazy5o3zv3 `
  -c pocReportingWebApiId=gxklbaklu9
```

Look up current API IDs with:

```powershell
aws apigateway get-rest-apis --region us-east-2 --query "items[?contains(name, 'hiveflow')].[name,id]" --output table
```

## DNS bootstrap (one time)

Set `manage_dns: true` in `config.yaml`, then deploy the DNS stack (and UI stacks if not already up):

```yaml
domain:
  zone_name: hive-flow-ai.com
  primary_hostname: hive-flow-ai.com
  alternate_hostnames: [www]
  manage_dns: true
  create_hosted_zone: true   # or hosted_zone_id when importing an existing zone
```

```powershell
cdk deploy -c scope=platform GlobalUiStack-dev PortalStack-dev GlobalDnsStack-dev
```

Copy `HostedZoneId` and `Route53NameServers` from **GlobalDnsStack** outputs, update Squarespace nameservers, then switch to steady-state config:

```yaml
domain:
  zone_name: hive-flow-ai.com
  primary_hostname: hive-flow-ai.com
  manage_dns: false
  hosted_zone_id: Z0833907O664KG7NO3CQ   # from HostedZoneId output
  create_hosted_zone: false
```

After bootstrap, deploy **`GlobalDnsStack-dev` only when DNS changes** (new hostname, certificate rotation). **`GlobalUiStack-dev` never includes Route 53 or ACM resources.**

Hosted zones and certificates created by GlobalDnsStack use `RemovalPolicy.RETAIN`.

## Recovering after DNS was removed from GlobalUiStack

If a GlobalUi deploy removed custom domains or tried to delete the hosted zone (before this split), restore DNS by deploying GlobalDnsStack with an imported zone:

```yaml
domain:
  manage_dns: true
  create_hosted_zone: false
  hosted_zone_id: Z0833907O664KG7NO3CQ
```

```powershell
cdk deploy GlobalDnsStack-dev
```

Then set `manage_dns: false` again for routine deploys.

Client reporting subdomains use a single `*.hive-flow-ai.com` wildcard record + ACM cert (created by **GlobalDnsStack**) that routes every `<client>.hive-flow-ai.com` to the shared **PortalStack** API. Adding a client needs no DNS or cert change. (`-c legacyReporting=true` re-creates the retired per-client single-name certs/domains/records for the cut-over/rollback window; an exact-match custom domain always wins over the wildcard.)

## Stack outputs

| Output | Stack | Meaning |
|---|---|---|
| `PrimarySiteUrl` | GlobalDnsStack | Main branded URL (`https://hive-flow-ai.com/`) |
| `Route53NameServers` | GlobalDnsStack | NS records to set at Squarespace |
| `HostedZoneId` | GlobalDnsStack | Route 53 zone ID for config |
| `SiteUrl` | GlobalUiStack | Branded URL from config (same hostname when DNS is configured) |
| `ApiGatewayUrl` | GlobalUiStack | Default `execute-api` URL |
| `ApiGatewayUrl` | PortalStack | Multi-tenant portal `execute-api` URL |
| `PortalWildcardSiteUrl` | GlobalDnsStack | `https://<client>.{zone}/` pattern served by the wildcard |

## Reusing an existing hosted zone

```yaml
ui:
  domain:
    zone_name: hive-flow-ai.com
    primary_hostname: hive-flow-ai.com
    alternate_hostnames: [www]
    manage_dns: true
    create_hosted_zone: false
    hosted_zone_id: Z1234567890ABC
```

Deploy `GlobalDnsStack-dev` once, then set `manage_dns: false`.

## Portal credentials

Custom domain does not change portal auth. After `GlobalUiStack` deploy, invite portal users by email:

```bash
hiveflow-dna portal-user invite --username jane --client-id poc --email jane@client.com
```

Use stack outputs `PortalUserPoolId` and `PortalUserPoolClientId` from **GlobalUiStack**.

## Platform admin (`admin.hive-flow-ai.com`)

Operational jobs (BC source-docs scrape / relationships / tags, and future data-source jobs) run from a separate admin site with its own Cognito pool.

1. Deploy `PlatformAdminStack-dev` (and once for DNS: `GlobalDnsStack-dev` with `admin_hostname: admin` in config).
2. Bootstrap `GlobalAdmin` using the same email as portal `AdminPOC`:

```powershell
hiveflow-dna admin-user bootstrap `
  --portal-user-pool-id <PortalUserPoolId from GlobalUiStack> `
  --admin-user-pool-id <AdminUserPoolId from PlatformAdminStack>
```

3. Sign in at `https://admin.hive-flow-ai.com/admin/login` as **GlobalAdmin**.

## Portal invite email (SES)

Portal invites use **Amazon SES** when `platform.environments.*.ui.portal.email.enabled` is `true` in `config.yaml`. CDK verifies `hive-flow-ai.com` in SES and adds DKIM records to the Route 53 hosted zone (`domain.hosted_zone_id`).

```yaml
portal:
  email:
    enabled: true
    from_address: noreply@hive-flow-ai.com
    from_name: HiveFlowAI
```

Deploy **GlobalUiStack** after enabling email:

```powershell
cdk deploy -c scope=platform GlobalUiStack-dev
```

**SES sandbox:** New SES accounts can only send to verified recipient addresses until you [request production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html) in the AWS console (SES → Account dashboard → Request production access). Until then, verify test inboxes in SES or set passwords manually with `aws cognito-idp admin-set-user-password`.

After deploy, stack output `PortalEmailFromAddress` confirms the sender. Resend a failed invite:

```powershell
aws cognito-idp admin-create-user `
  --user-pool-id <PortalUserPoolId> `
  --username jerem `
  --user-attributes Name=email,Value=you@hive-flow-ai.com Name=email_verified,Value=true `
  --message-action RESEND `
  --desired-delivery-mediums EMAIL `
  --region us-east-2
```
