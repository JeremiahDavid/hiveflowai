# Entity Resolution

How the hiveflow layer links the **same real-world thing** across ERP, accounting, Excel, and unstructured inputs — when IDs, names, and timing don't align.

> **Framing note.** Examples below use a manufacturer/distributor ICP that is no longer the committed direction — see [product-vision.md](../product-vision.md). The **capability is universal-core and, if anything, more central under the current framing**: cross-system identity resolution is precisely what makes a *centralized* data model possible, and every industry framework pack declares its own identity keys on top of this machinery (patient identity across PMS/imaging/billing, for example, is the same problem as customer identity across ERP/QB).

---

## Why this is the core moat

Most operational exceptions break at the join:

| Exception | Broken without entity resolution |
|---|---|
| Unbilled WIP | Shipped in ERP, invoice in QB under different customer name |
| Late job $ by customer | Job in ERP, AR in QB, names don't match |
| Customer margin rollup | Revenue in accounting, costs in ERP, no stable customer key |
| Excel shortage on Job 4412 | Spreadsheet says "4412", ERP job number is "JO-004412" |

ERP vendors won't solve QuickBooks matching. iPaaS does exact keys. HiveFlow does **fuzzy, confidence-scored linking** with human override memory.

---

## Entity types and keys

### Canonical ID strategy

Every entity gets an internal **`hiveflow_id`** (stable within tenant). Source system IDs are **attributes**, not primary keys.

```yaml
Customer:
  hiveflow_id: cust_a1b2c3
  source_ids:
    erp: "104"
    qbo: "87"
  normalized_name: "acme corporation"
  display_name: "Acme Corp"          # prefer ERP display
```

```yaml
Job:
  hiveflow_id: job_x9y8z7
  source_ids:
    erp: "JO-004412"
  customer_hiveflow_id: cust_a1b2c3
```

```yaml
Invoice:
  hiveflow_id: inv_m5n6o7
  source_ids:
    qbo: "9921"
  customer_hiveflow_id: cust_a1b2c3
  job_hiveflow_id: job_x9y8z7            # optional, linked
```

---

## Customer matching (ERP ↔ accounting)

### Signal stack (weighted)

| Signal | Weight | Notes |
|---|---|---|
| Exact normalized name | High | Strip Inc, LLC, punctuation, case |
| Tax ID / EIN | Very high | Rarely available |
| Billing address (street + zip) | High | Often matches when name doesn't |
| Phone / email domain | Medium | B2B useful |
| Payment terms + credit limit | Low | Tie-breaker |
| Historical confirmed links | Very high | Tenant memory |

### Matching tiers

| Tier | Confidence | Action |
|---|---|---|
| **A — Deterministic** | ≥ 0.95 | Auto-link (exact ID map file, EIN, prior confirmation) |
| **B — Strong fuzzy** | 0.85 – 0.94 | Auto-link; log for spot audit |
| **C — Weak fuzzy** | 0.70 – 0.84 | Review queue |
| **D — No match** | < 0.70 | Hold cross-system exceptions; surface "unmatched customer" internally |

### AI role

- Suggest matches for tier B/C from name variants ("ACME" / "Acme Industries")
- Propose **alias entries** after human confirm
- Detect **split records** (same customer, two ERP IDs) — flag, don't auto-merge without review

### Never auto-merge

- Customers with conflicting open AR totals > threshold
- Names match but addresses differ materially
- Active jobs on one record, invoices on another — route to review

---

## Job ↔ invoice linking

### Signals

| Signal | Weight |
|---|---|
| Shared job number on invoice memo/line | Very high |
| Exact amount match (ship $ = invoice $) | High |
| Customer hiveflow_id match + amount within tolerance | High |
| Ship date → invoice date within N days | Medium |
| PO number match | Medium |
| One-to-many (partial invoices) | Pattern rule |

### Partial and progress billing

Job shops often invoice in milestones. Rules:

- Allow **one job → many invoices** (sum tracked)
- `unbilled_wip` = shipped/job-complete amount minus **linked invoice sum** above confidence threshold
- Never infer uninvoiced **dollar amount** without ship/value signal from ERP

### Confidence outcomes

| Outcome | Briefing behavior |
|---|---|
| Linked ≥ 0.85 | Unbilled logic applies |
| Linked 0.70–0.84 | Review queue; optional "possible unbilled" with flag |
| Linked < 0.70 | Do not show unbilled exception; review only |

---

## Job ↔ Excel / unstructured rows

Shadow ops data (shortage lists, hot jobs) often uses informal job references.

### Resolution path

1. **Exact match** on job number field (after normalization strip prefixes)
2. **Fuzzy match** on job + customer name in same row
3. **AI extract** from free text ("4412 acme bracket") → candidate job list
4. If multiple candidates → review queue, not briefing

### Normalization rules (examples)

```
JO-004412 → 4412
Job #4412  → 4412
004412     → 4412
```

Playbook per ERP family defines prefix/suffix patterns.

---

## Status and lifecycle resolution

Entity resolution isn't only IDs — it's **what state the job is really in**.

### Effective status derivation

| Inputs | Effective status |
|---|---|
| `ship_date` set, status Open | `shipped` (flag raw conflict) |
| `ship_date` set, invoice linked | `shipped_invoiced` |
| No activity 120d, status Open | `stale_open` → review |
| Status Closed, no ship_date | `closed_unknown` → review |

Effective status drives late-job and unbilled logic — not raw ERP status alone.

---

## Conflict objects

When systems disagree, create an explicit **conflict record** (don't pick silently):

```yaml
conflict_id: conf-882
type: status_date_conflict
job_hiveflow_id: job_x9y8z7
signals:
  erp_status: Open
  erp_ship_date: 2026-07-05
  qbo_invoice: null
summary: "Job appears shipped but open in ERP and uninvoiced in QB"
recommended_review: controller
```

Conflicts feed review queue and may generate **safe** exceptions ("possible unbilled — confirm") with lower priority than high-confidence ones.

---

## Tenant memory (learning loop)

Human actions persist and improve future runs:

| Action | Effect |
|---|---|
| Confirm customer link | Permanent alias + source ID map |
| Reject link | Block that pairing; optional negative rule |
| Snooze exception | Suppress N days; doesn't change graph |
| Add alias | Name normalization table |
| Set policy | e.g. "never use tier 3 promise date in briefing" |

**Cross-tenant learning (later):** ERP-family playbook improvements from aggregated mapping drift patterns — **never** share customer/job data across tenants.

---

## AI-assisted matching — guardrails

| Allowed | Not allowed |
|---|---|
| Rank candidate matches with explanation | Auto-link financial records below tier B at launch |
| Propose new alias from confirmed pattern | Merge customers with material AR conflicts |
| Parse unstructured job refs | Invent missing invoice or job IDs |
| Learn from overrides within tenant | Cross-tenant entity sharing |

Every auto-link above tier B stores **retrieval evidence** (which signals fired) for provenance.

---

## Metrics

| Metric | Definition |
|---|---|
| Customer match rate | % ERP customers with QB link ≥ 0.85 |
| Job-invoice link rate | % closed/shipped jobs with invoice coverage |
| Review queue volume | Items created per run |
| Override rate | Human corrections / total auto-links |
| False link rate | Rejected confirms within 30 days |

Target: high auto-link rate with **low false positive** on financial exceptions — prefer review over wrong briefing.

---

## Related

- [reconciliation-engine.md](./reconciliation-engine.md) — pipeline placement
- [confidence-and-provenance.md](./confidence-and-provenance.md) — scoring thresholds
- [v1-scope.md](./v1-scope.md) — what's in first release
