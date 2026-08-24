# HiveFlow Layer — v1 Scope

What the reconciliation engine **ships in v1**, what waits, and how it unlocks the user-facing product (Option A → Option B).

> **Framing note (superseded positioning).** This doc scopes v1 around a **product manufacturer / distributor** ICP and an exception-briefing deliverable. That ICP is no longer the committed direction — see [product-vision.md](../product-vision.md) for the current north star (a centralized data model across systems, with industry-specific connectors and industry-specific model frameworks). Manufacturing/distribution remains a **candidate vertical**, not the chosen one.
>
> **Still valid:** the pipeline capability list, canonical entity set, automate-vs-review defaults, onboarding fit gate, and metrics targets — these describe engineering behavior that is industry-agnostic. The "Expansion path (industry repeatability)" section is the earliest sketch of what [product-vision.md](../product-vision.md) now calls **industry framework packs**.
>
> **Superseded:** the ICP selection gate, the specific source-priority table, and the Option A → Option B product sequencing.

**Companion:** [data-lake-architecture.md](./data-lake-architecture.md) — AWS storage layout for multi-source ingest.

---

## v1 goal

Prove that invisible hiveflow can produce **trusted daily exceptions** for product manufacturers or distributors—across either a split ops + QuickBooks stack or cross-module NetSuite/BC data—without becoming custom ETL or report-building per client.

**Success:** First tenant gets a published snapshot within **5 business days** of access; morning briefing false-positive rate **< 5%** after tuning week.

---

## In scope (v1)

### Sources

| Source | Priority | Pattern |
|---|---|---|
| Phase 1 A+B system family (choose one) | Discovery gate | NetSuite or BC; Fishbowl/Cin7 only if a native-integration gap validates |
| QuickBooks Online | Foundation in progress | Retain existing work; not evidence that a QBO-path ICP should win |
| QuickBooks Desktop | Conditional | Scheduled export |
| Excel / Google Sheets (templated) | Foundation | Column map template per file type |

**Selection gate:** Score Business Central and NetSuite by reachable accounts × recurring pain × willingness to pay × access × repeatability. Admit Fishbowl/Cin7 only when discovery quantifies failures or exceptions their native QBO/Xero/commerce integrations and status dashboards do not solve. Secure one design partner, then build exactly one Phase 1 family.

### Pipeline capabilities

| Capability | v1 |
|---|---|
| Ingest + raw storage | Yes |
| Playbook-based column map | Yes |
| AI-suggested map drift (internal approve) | Yes |
| Customer match across selected sources/modules | Yes |
| Fulfillment ↔ invoice link | Yes |
| Effective status (shipped vs open) | Yes |
| Promise date fallback tier 1–2 | Yes |
| Promise date tier 3 inference | Optional; strict tenants off by default |
| Batch quality gate + suppression | Yes |
| Review queue (admin) | Yes |
| Tenant memory (aliases, confirmed links) | Yes |
| Provenance chain per exception | Yes |
| Unstructured PDF/email | **No** — v1.1 |

### Canonical entities (v1)

- Customer
- Sales order / line
- Fulfillment / shipment
- Purchase or replenishment reference where the launch Signal requires it
- Invoice / AR open balance
- Inventory item / snapshot

### Downstream exceptions enabled

These feed the user-facing briefing once hiveflow publishes:

| Exception | HiveFlow dependencies |
|---|---|
| Unbilled fulfillment | Shipped/complete event + fulfillment–invoice link |
| Partial billing mismatch | Order/fulfillment/invoice line quantities and dollars |
| Past-due AR | Accounting-system invoice and payment entities |
| Backorder / OTIF risk | Promise date, fulfillment state, inventory/replenishment context |
| Inventory or margin exception | Item movement/cost/revenue facts; include only if selected launch Signal |
| Customer concentration | Revenue rollup via customer link |

---

## Out of scope (v1)

| Item | Target |
|---|---|
| Real-time MES / shop floor | Never v1 |
| Write-back to ERP/QB | v2+ if ever |
| PDF / email unstructured ingest | v1.1 |
| CRM / quoting module | v1.2 |
| Multi-entity consolidation | PS |
| Custom costing / overhead allocation | PS |
| Customer-facing review UI | v1.1 (admin email/link OK v1) |
| Cross-tenant benchmarking | v2+ |
| Full Option B quote-intelligence | v1.2 (basic item/customer margin only if selected) |

---

## Automate vs review (v1 defaults)

| Decision | Auto | Review |
|---|---|---|
| Customer match tier A/B | ✓ | |
| Customer match tier C/D | | ✓ |
| Fulfillment–invoice link tier B+ | ✓ | |
| Fulfillment–invoice link tier C | | ✓ |
| Schema map change | | ✓ (internal) |
| Batch confidence < 0.75 | Suppress | ✓ (internal) |
| Promise date fallback tier 2 | ✓ | |
| Promise date tier 3 inference | | ✓ or exclude |
| Excel → business entity link tier B+ | ✓ | |
| Excel → business entity link tier C | | ✓ |

---

## User-facing product coupling

### Phase 1 — Exception briefing (Option A)

HiveFlow v1 must reliably power:

1. One discovery-selected, dollarized A+B exception queue
2. Provenance across each source record or ERP module
3. Historical snapshot and ranked ownership
4. Optional: Excel/satellite context that materially reduces false positives

Delivery: daily email + minimal detail links. Provenance one click.

### Phase 2 — Profitability layer (Option B)

Add on same hiveflow snapshot:

1. Item / order margin table
2. Customer rollup
3. Quote or expected price vs actual when source fields exist
4. "Bottom quartile" shortlist → folds into briefing as exception type

Requires trusted cost and revenue fields in the selected family plus `cost_status` (provisional vs final).

---

## Onboarding fit gate (must pass before hiveflow build)

Score at discovery — **≥ 7/10** to proceed:

| # | Gate |
|---|---|
| 1 | API, query, or reliable export feasible within 5 days |
| 2 | Accounting and operations modules/systems are identified |
| 3 | Stable order, fulfillment, and invoice references exist or can be linked |
| 4 | Fields required by the launch Signal are sufficiently populated |
| 5 | A recurring cross-system or cross-module exception has material dollar exposure |
| 6 | Named admin for review queue (controller or ops lead) |
| 7 | Client accepts 10-day tuning period |
| 8 | No air-gap / ITAR blocker |
| 9 | Primary contact responds within 1 business day |
| 10 | Client accepts a packaged Signal definition rather than open-ended custom reporting |

---

## Playbook deliverables (v1 engineering)

Per selected system-family playbook:

- [ ] Named APIs, queries, or standard reports for orders, fulfillments, invoices, inventory, and AR as required
- [ ] Column map → canonical model
- [ ] Status vocabulary map
- [ ] Order / fulfillment / invoice key normalization rules
- [ ] Known quirks doc (1 pager)
- [ ] Sample anonymized extract for CI tests

Per accounting path (QBO/QBD or full-ERP finance module):

- [ ] Customer list, invoice lines, payments, and AR aging extracts
- [ ] Customer ID / name map strategy

---

## Week-by-week delivery (new tenant)

| Day | Milestone |
|---|---|
| 1–2 | Access granted; playbooks selected; first raw extracts |
| 3 | First parse + map; customer match draft |
| 4 | First entity graph; review queue populated |
| 5 | First **published snapshot** (internal) |
| 6–10 | Tuning: false positive fixes, alias confirms |
| 10+ | Briefing goes live to end users |

---

## Internal tooling (v1)

Minimum ops tools for you — not client product:

| Tool | Purpose |
|---|---|
| Batch monitor | Missing files, row count anomalies |
| Review queue admin | Confirm/reject links |
| Provenance debugger | Trace any surfaced fact |
| Playbook editor | Version column maps |
| Tenant memory viewer | Aliases, policies, overrides |

---

## Expansion path (industry repeatability)

Same hiveflow engine, new **definition packs**:

| Pack | Changes |
|---|---|
| `product_mfg` | Order, finished goods, inventory, cost, and margin semantics |
| `distribution` | Order lines, fill rate, OTIF |
| `trade_contractor` | Change orders, job WIP |
| `field_service` | Work orders, callbacks |

System-family expansion is separate from industry-pack expansion. Do not add a second A+B connector until the first family produces repeatable paid Signals.

Entity types and pipeline stay stable; exception catalog and field maps swap per pack.

---

## v1 metrics dashboard

Track weekly:

| Metric | Target |
|---|---|
| Tenants with daily publish success | ≥ 98% |
| Median onboarding days to first publish | ≤ 5 |
| Customer auto-match rate | ≥ 95% |
| Fulfillment–invoice auto-link rate | ≥ 85% |
| Briefing false positive rate (client snooze/reject) | < 5% |
| Review items per tenant per week (steady state) | < 10 |
| HiveFlow-hours per onboarding | Baseline then ↓ with playbooks |

---

## Related

- [reconciliation-engine.md](./reconciliation-engine.md)
