# Confidence & Provenance

How the hiveflow layer decides **what to trust**, **what to hold back**, and **how to explain every surfaced fact** — even when AI is invisible to the user.

> **Framing note.** Examples reference a manufacturer/distributor ICP that is no longer the committed direction — see [product-vision.md](../product-vision.md). The **spec is universal-core and unchanged**. It also gets more load-bearing under the current framing: a model consumed by AI agents that act on operations needs confidence and provenance more than a model consumed only by a human reading a chart.

---

## Principle

> **Better no briefing than a wrong briefing.**

Confidence scoring and provenance aren't compliance overhead — they're the product. Small manufacturers will forgive missing data; they won't forgive confidently wrong late jobs or dollar amounts.

---

## Three confidence layers

### 1. Field confidence

How trustworthy is a single field value on a canonical entity?

| Level | Code | Meaning | Example |
|---|---|---|---|
| **Direct** | `D` | Copied from source, parse OK | ERP `DueDate` |
| **Mapped** | `M` | Field mapped via playbook | Source ship/due field → `promise_date` |
| **Fallback** | `F` | Rule-based substitute | `promise_date` := `due_date` when null |
| **Inferred** | `I` | Model-assisted, signals combined | Promise date from customer history |
| **Missing** | `—` | Not available | No date signal at all |

**Briefing policy (default):**

| Code | Use in late-job logic? | User-visible flag? |
|---|---|---|
| D, M | Yes | No |
| F | Yes | Optional footnote |
| I | Tenant policy | **Yes — always** |
| — | Exclude from date-based exceptions | N/A |

---

### 2. Link confidence

How trustworthy is a relationship (customer match, fulfillment–invoice link)?

| Range | Tier | Action |
|---|---|---|
| 0.95 – 1.00 | A | Auto-link silently |
| 0.85 – 0.94 | B | Auto-link; audit log |
| 0.70 – 0.84 | C | Review queue; no financial exception |
| < 0.70 | D | Unlinked; internal hold |

Financial exceptions (unbilled WIP, customer margin rollup) require **link tier B or better** unless tenant opts into flagged "possible" state.

---

### 3. Batch confidence

Is today's extract fit to publish?

Computed from:

| Signal | Weight |
|---|---|
| File received on schedule | Required |
| Row count vs 7-day median | High |
| Parse error rate | High |
| Customer match rate drop vs baseline | High |
| New unmatched jobs spike | Medium |
| Schema drift detected | High — may block |

**Batch score:** 0.0 – 1.0

| Score | Action |
|---|---|
| ≥ 0.90 | Publish full snapshot |
| 0.75 – 0.89 | Publish with warnings; suppress sensitive exceptions |
| < 0.75 | **Do not publish briefing**; alert internal ops + tenant admin |

---

## Provenance model

Every fact the insight product surfaces must trace to a **provenance chain**.

### Provenance object

```yaml
provenance_id: prov-77102
fact_type: late_job
entity_ref: job_x9y8z7
display: "Job 4412 — 8 days late ($18,400)"

chain:
  - step: source
    system: jobboss
    report: open_jobs
    field: DueDate
    raw_value: "2026-07-01"
    batch_id: batch-2026-07-17-erp

  - step: map
    rule: jobboss_promise_date_map
    note: "SchedDate null; DueDate mapped to promise_date fallback"

  - step: normalize
    rule: promise_date_fallback_tier_2
    output_field: effective_promise_date
    value: "2026-07-01"
    field_confidence: F

  - step: derive
    rule: days_late
    inputs: [effective_promise_date, as_of_date]
    value: 8

  - step: link
    entity: customer
    customer_hiveflow_id: cust_a1b2c3
    link_confidence: 0.97
    link_tier: B
```

### User-facing explanation (generated from chain — not free-form LLM)

Template-based narrative for trust:

> **Job 4412** is **8 days past due date** (Jul 1). Due date used because promise date wasn't in JobBOSS. **Customer:** Acme Corp. **Job value:** $18,400.  
> *As of today 6:00 AM.*

For inferred fields, append:

> *⚠ Promise date estimated — confirm in ERP if close to deadline.*

---

## Suppression rules

Hard gates before exceptions reach the briefing:

| Rule ID | Condition | Effect |
|---|---|---|
| SUP-001 | Batch confidence < 0.75 | Suppress all auto exceptions |
| SUP-002 | Link tier C or D on fulfillment–invoice | Suppress unbilled $ exception |
| SUP-003 | Field confidence I on promise date + tenant strict mode | Exclude from late list |
| SUP-004 | Job in review queue (unresolved) | Exclude until resolved |
| SUP-005 | Row parse error on driving field | Exclude entity; log |
| SUP-006 | Snooze active on entity | Suppress until expiry |

---

## Degraded modes

When things go wrong, behave predictably:

| Mode | Trigger | User experience |
|---|---|---|
| **Normal** | Batch OK | Full briefing |
| **Partial** | Secondary source missing (Excel drop) | Briefing minus shortage section; note in footer |
| **Caution** | Batch 0.75–0.89 | Briefing with banner: "Data quality lower than usual — verify critical items" |
| **Hold** | Batch < 0.75 or ERP missing | Email: "Today's briefing paused — [ERP] data not received" |
| **First run** | New tenant, tuning period | Banner: "Initial tuning week — please flag errors" |

---

## Audit trail (internal + client admin)

Retain for **24 months** (align with data retention policy):

- Raw batches (immutable)
- Mapping manifest version
- Entity graph snapshot per run
- All review queue actions
- Suppression log with rule IDs
- Provenance chains for every surfaced exception

Client admin view (future): "Why was this flagged?" → provenance chain, no raw AI chat.

---

## AI-generated explanations — rules

When LLM assists narrative:

| Rule | Requirement |
|---|---|
| Grounding | Only facts present in provenance chain |
| No new numbers | LLM may not introduce amounts, dates, or counts |
| Template fallback | If LLM fails validation → static template |
| Validation | Post-check: every $ and date in text exists in chain |
| Marking | Inferred fields always labeled in text |

**Prefer template + slots over free-form prose for v1 financial exceptions.**

---

## Tuning period (new tenant)

Default **10 business days** after first publish:

- Lower auto-link thresholds discouraged — prefer review
- All snooze/reject feedback captured
- Weekly internal review of false positives
- Tenant policy locked at end of tuning (strict vs standard inference display)

---

## Metrics

| Metric | Target |
|---|---|
| Briefing suppression rate | < 2% of business days (excluding missing files) |
| User-reported false exception rate | < 5% |
| Provenance coverage | 100% of surfaced exceptions |
| Review queue SLA | Client admin resolves within 48h (guideline) |

---

## Related

- [reconciliation-engine.md](./reconciliation-engine.md)
- [entity-resolution.md](./entity-resolution.md)
- [ai-boundaries.md](./ai-boundaries.md)
