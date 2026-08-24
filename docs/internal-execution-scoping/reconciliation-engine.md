# Reconciliation Engine

Core specification for the hiveflow layer: how raw inputs become **trusted, ranked operational objects** ready for briefings and exception logic.

**Audience:** Internal product and engineering. Not customer-facing.

> **Framing note.** Examples in this doc are drawn from a manufacturer/distributor ICP that is no longer the committed direction — see [product-vision.md](../product-vision.md). The **engineering spec is unchanged and industry-agnostic**: entity linking, lifecycle normalization, confidence scoring, provenance, and quality gates are universal-core behavior that every industry framework pack depends on. Read the examples as illustrative, not as scope.

**Companion:** [data-lake-architecture.md](./data-lake-architecture.md) — S3 layout, multi-connector ingest, Glue/Athena, bronze vs gold storage.

---

## Purpose

Transform fragmented, dirty, incomplete data from 2–3 client systems into a **single canonical ops model** with:

- Linked entities (customer, job, order, invoice)
- Normalized lifecycle states
- Explicit confidence scores
- Full provenance (source field, inference rule, or human override)
- Quality gates before anything reaches the user-facing product

The engine does **not** render UI, send email, or rank business exceptions. It produces **clean, explainable facts** that downstream rules consume.

---

## Inputs

### Primary (v1)

| Source | Typical systems | Ingest pattern |
|---|---|---|
| **Operations / ERP** | Fishbowl/Cin7, NetSuite operations modules, Dynamics BC operations modules | Scheduled export, ODBC read, SuiteQL/OData, or API |
| **Accounting** | QuickBooks Online/Desktop or NetSuite/BC finance modules | API or scheduled export |
| **File / shadow ops** | Excel, Google Sheets | Scheduled drop to inbox (S3, email parser, upload) |

### Secondary (v1.1+)

| Source | Use |
|---|---|
| Email (structured notifications) | Ship confirmations, expedite requests, customer date changes |
| PDF | PO copies, packing lists, shortage reports |
| CRM | Quotes, pipeline (when not in ERP) |

### Input contract (per source)

Each ingest produces a **raw batch**:

```yaml
batch_id: "2026-07-17-0600-acme-erp"
tenant_id: acme
source_system: jobboss
source_family: jobboss_v2024          # playbook key
extract_type: open_jobs_report        # named report in playbook
received_at: 2026-07-17T06:00:00Z
row_count: 847
file_hash: sha256:...
raw_location: s3://...                # internal only
schema_version: 1
```

---

## Pipeline stages

```
INGEST → PARSE → MAP → MATCH → NORMALIZE → INFER → SCORE → GATE → PUBLISH
```

### Stage 1: Ingest

- Receive file/API payload per schedule (default: daily overnight; configurable)
- Validate file presence, size, row count vs historical baseline
- Store raw immutable copy in the tenant **raw bucket** (bronze layer) — see [data-lake-architecture.md](./data-lake-architecture.md)
- Emit ingest events and anomalies (missing file, 90% row drop, duplicate batch)

**Failure mode:** If primary ERP batch missing → **degraded mode** (see [confidence-and-provenance.md](./confidence-and-provenance.md)); do not publish full briefing.

---

### Stage 2: Parse

- Detect format (CSV, XLSX, JSON, API response)
- Apply ERP-family parser profile (delimiter, header row, date formats)
- Extract typed columns with parse errors logged per row

**Output:** `raw_rows[]` with source column names preserved.

---

### Stage 3: Map (schema alignment)

Map source columns → **canonical model fields** using:

1. **Playbook defaults** (source-specific ship/due field → canonical `promise_date`)
2. **Tenant overrides** (client renamed column in custom report)
3. **AI-assisted mapping** (sample-based suggestion when report layout drifts — human approves before production)

**Canonical entities (v1):**

| Entity | Key fields |
|---|---|
| `Customer` | `customer_id`, `name`, `normalized_name`, `terms`, `credit_limit` |
| `Job` / `WorkOrder` | `job_id`, `customer_id`, `status_raw`, `order_date`, `due_date`, `promise_date`, `ship_date`, `job_value`, `wip_value` |
| `JobCost` | `job_id`, `revenue`, `material_cost`, `labor_cost`, `outside_proc_cost`, `total_cost`, `margin`, `cost_as_of` |
| `Invoice` | `invoice_id`, `customer_id`, `job_id?`, `invoice_date`, `amount`, `open_balance` |
| `Shipment` | `job_id`, `ship_date`, `qty`, `amount` |
| `InventoryException` | `sku`, `job_id?`, `exception_type`, `notes` (from ERP MRP or Excel) |

**Output:** `mapped_rows[]` + mapping manifest (which source column fed which canonical field).

---

### Stage 4: Match (entity resolution)

Link records **across systems** into canonical IDs. See [entity-resolution.md](./entity-resolution.md).

**Core links (v1):**

| Link | Systems | Why it matters |
|---|---|---|
| Customer ERP ↔ Customer QB | ERP + accounting | Margin rollup, AR exceptions |
| Job ↔ Invoice(s) | ERP + accounting | Unbilled WIP, revenue timing |
| Job ↔ Customer | Within ERP + cross-check QB | Late job $ impact by customer |
| Excel row ↔ Job | Shadow ops + ERP | Shortage / hot list in same queue |

**Output:** `entity_graph` — nodes (entities) + edges (links) with `link_confidence` and `link_method`.

---

### Stage 5: Normalize (lifecycle and semantics)

Convert raw status strings and dates into **effective operational state** using industry definition packs.

**Examples:**

| Raw signal | Normalized state | Rule tier |
|---|---|---|
| `status=Open`, `ship_date` populated | `effective_status=shipped` | Rule (high confidence) |
| `promise_date` null, `due_date` present | `effective_promise_date=due_date` | Fallback tier 2 |
| `promise_date` null, history exists | `effective_promise_date=inferred` | Inference tier 3 — flagged |
| Job closed in ERP, costs updated 5 days later | `cost_status=provisional` | Rule |

**Output:** Normalized entities with `field_provenance` per derived field.

---

### Stage 6: Infer (gap filling — conservative)

Fill **non-financial** gaps where rules allow. See [ai-boundaries.md](./ai-boundaries.md).

**Allowed inference (v1):**

- Effective fulfillment/order status from composite signals
- Promise date fallback hierarchy
- Customer alias suggestions
- Fulfillment–invoice link suggestions when IDs are missing but amounts/dates align
- Classification of unstructured rows (Excel hold/shortage → order or item link)

**Forbidden inference:**

- Inventing cost, revenue, or open balance amounts
- Fabricating ship dates or invoice dates with no supporting signal
- Presenting guessed margin as trusted item/customer truth

**Output:** Enriched entities + `inference_log[]`.

---

### Stage 7: Score (confidence)

Assign confidence at three levels:

1. **Field confidence** — is this value direct from source or inferred?
2. **Link confidence** — is this customer/job/invoice match reliable?
3. **Batch confidence** — is today's extract trustworthy overall?

See [confidence-and-provenance.md](./confidence-and-provenance.md).

**Output:** Scored entity graph + batch quality report.

---

### Stage 8: Gate (publish or suppress)

Decision rules before publishing to downstream product:

| Condition | Action |
|---|---|
| Batch confidence below threshold | Suppress briefing; alert ops team |
| Link confidence low for financial exception | Route to **review queue**, not briefing |
| Field inferred tier 3 on date driving "late" | Include with visible flag, or exclude per tenant policy |
| Entity conflict unresolved | Hold affected exceptions |

**Output:** `published_snapshot` + `review_queue_items[]` + `suppression_log`.

---

### Stage 9: Publish

Write versioned snapshot to **curated bucket** (gold layer — see [data-lake-architecture.md](./data-lake-architecture.md)):

- Canonical entities for tenant as-of timestamp
- Provenance bundle (reproducible from raw + manifest)
- Change delta vs prior snapshot (new late jobs, new match failures)

Downstream **insight product** reads only `published_snapshot` from curated storage — never raw.

---

## Core output objects

### Published snapshot (per tenant, per run)

```yaml
snapshot_id: snap-2026-07-17-acme
tenant_id: acme
as_of: 2026-07-17T06:00:00Z
batch_confidence: 0.92
entities:
  customers: [...]
  jobs: [...]
  invoices: [...]
  links: [...]
quality:
  customer_match_rate: 0.96
  job_invoice_link_rate: 0.88
  inferred_promise_date_pct: 0.12
  rows_parse_errors: 3
```

### Review queue item

```yaml
review_id: rev-1042
type: job_invoice_link
priority: high
reason: "Shipped job 4412 — no invoice match above 0.85 confidence"
candidate_links:
  - invoice_id: INV-9921
    confidence: 0.72
    signals: ["amount within 2%", "same customer", "ship date +3 days"]
suggested_action: confirm_link | reject | create_exception
assigned_role: controller
```

### Provenance record (attached to any surfaced fact)

```yaml
fact: "Job 4412 is 8 days past effective promise date"
sources:
  - system: jobboss
    field: SchedDate
    value: null
  - system: jobboss
    field: DueDate
    value: 2026-07-01
inference:
  rule: promise_date_fallback_tier_2
  effective_promise_date: 2026-07-01
  confidence: 0.85
  inferred: false
```

---

## Human review queue

Low-confidence items land here **before** they affect briefings (or appear with explicit "needs confirmation" state).

### Queue types (v1)

| Type | Trigger | Default assignee |
|---|---|---|
| `customer_match` | Cross-system match < 0.90 | Controller / admin |
| `job_invoice_link` | Shipped job, no invoice ≥ 0.85 | Controller |
| `schema_drift` | AI mapping suggestion pending | Internal ops (you) |
| `batch_quality` | Batch confidence < threshold | Internal ops |
| `status_conflict` | Raw vs effective status disagree materially | Ops manager |

### Review UX (internal / admin — not end-customer v1)

Minimal UI or structured email:

- Show candidates and signals
- Actions: **Confirm**, **Reject**, **Alias** (customer), **Snooze 7d**
- Overrides write to `tenant_memory` and apply on next run

### Override persistence

```yaml
tenant_memory:
  customer_aliases:
    - canonical: CUST-104
      aliases: ["ACME CORP", "Acme Corporation", "Acme Corp."]
  confirmed_links:
    - job_id: JOB-4412
      invoice_id: INV-9921
      confirmed_by: jane@client.com
      confirmed_at: 2026-07-10
  field_policies:
    promise_date_fallback: tier_2_only   # never show tier_3 inferred in briefing
```

---

## Downstream contract (to insight product)

The insight product consumes `published_snapshot` and computes:

- Unbilled fulfillment (shipped/completed events without linked invoice above confidence threshold)
- Partial fulfillment / billing mismatch
- Past-due AR (from accounting entities)
- Backorder / OTIF risk
- Inventory or margin outliers when required by the selected Signal

**Insight product must not re-implement matching or inference.** If hiveflow didn't publish it with sufficient confidence, it doesn't ship.

---

## ERP-family playbooks

Scale depends on **playbooks**, not per-client custom code.

| Playbook key | Covers | v1 priority |
|---|---|---|
| `qbo_v*` | Customers, invoices, payments, AR aging | Foundation in progress |
| `generic_excel_v1` | Column header detection + manual template | Foundation |
| `fishbowl_v*` or `cin7_v*` | Orders, fulfillments, inventory | Validate only; require native-integration/control gap |
| `bc_v*` | Cross-module orders, fulfillments, invoices, inventory, AR | Phase 1 full-ERP candidate |
| `netsuite_v*` | Cross-module orders, fulfillments, invoices, inventory, AR | Phase 1 full-ERP candidate |

Each playbook defines: extract names, column map, status vocabulary, date formats, known quirks.

---

## Non-goals (engine)

- Real-time shop-floor streaming
- Write-back to ERP or accounting
- Audited financial statements
- Unlimited custom business logic per tenant
- Customer-facing "data management" UI

---

## Success metrics (internal)

| Metric | Target (v1) |
|---|---|
| Time to first published snapshot (new tenant) | ≤ 5 business days from access |
| Customer match rate across selected sources/modules | ≥ 95% auto, remainder in review |
| Fulfillment–invoice link rate | ≥ 85% auto |
| False positive rate on launch Signal | < 5% (measured via snooze/reject) |
| Batch publish success rate | ≥ 98% daily |
| Manual review items per tenant per week | Trending down; < 10 at steady state |

---

## Related documents

- [entity-resolution.md](./entity-resolution.md)
- [confidence-and-provenance.md](./confidence-and-provenance.md)
- [ai-boundaries.md](./ai-boundaries.md)
- [v1-scope.md](./v1-scope.md)
