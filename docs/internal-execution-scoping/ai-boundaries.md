# AI Boundaries

What AI is for in the hiveflow layer, what it must **never** do, and how to stay differentiated without becoming a liability.

**Positioning:** AI is **implementation advantage**, not the product headline. Market outcomes; use AI to deliver them reliably across messy SMB ops data.

> **Framing note.** Written against a manufacturer/distributor ICP that is no longer the committed direction — see [product-vision.md](../product-vision.md). The **guardrails are universal-core and still binding**: AI assists model *authoring* (schema mapping, entity-resolution ranking, KPI drafting), never scheduled *refresh* — approved logic is pinned and replayed verbatim. One thing the current framing adds: AI agents are now an explicit **consumer** of the finished model, not only a tool inside the pipeline. The boundaries here govern AI *inside* hiveflow; agent-facing consumption is a separate surface.

---

## Strategic role of AI

AI sits **between systems**, not on the front end:

```
Systems (messy) → [AI-assisted hiveflow] → Trusted facts → Briefings (clean)
```

### Primary AI jobs (in priority order)

| # | Job | Why AI vs rules alone |
|---|---|---|
| 1 | **Schema / column mapping** when reports drift | Layout changes break brittle ETL |
| 2 | **Entity resolution** ranking and alias suggestion | Name chaos across ERP + QB |
| 3 | **Unstructured extraction** (Excel, PDF, email) | Shadow ops isn't in APIs |
| 4 | **Effective status inference** from composite signals | Status fields lie |
| 5 | **Conflict explanation** (internal + user templates) | Cross-system narrative |
| 6 | **Data quality anomaly detection** | Batch drift, row drops |

### Secondary (later)

| Job | Notes |
|---|---|
| Similar job clustering for quoting | Needs closed-job history |
| Peer benchmarking | Cross-tenant, anonymized — high bar |
| Action draft text (collections email) | Front-edge product, not hiveflow core |

---

## Hard boundaries — what AI must NEVER do

| Forbidden | Why |
|---|---|
| **Invent dollar amounts** (revenue, cost, AR balance) | Trust death; legal exposure |
| **Fabricate dates** with no source signal | False late jobs |
| **Auto-post write-back** to ERP/accounting (v1) | Ops risk |
| **Present inferred margin as audited truth** | Controller revolt |
| **Auto-merge customers** with conflicting AR | Collections go to wrong account |
| **Publish briefing when batch confidence fails** | Silent harm |
| **Free-form LLM answers** on financial facts without provenance validation | Hallucination |

**Mantra:** AI **connects and classifies**; it does **not** create financial reality.

---

## Allowed inference — detailed rules

### Dates

| Scenario | Allowed | Confidence | Briefing |
|---|---|---|---|
| Promise null, due present | Use due as fallback | F | Yes + optional note |
| Both null, customer avg lead time | Infer expected date | I | Flag or exclude (policy) |
| Email says "ship Friday" | Parse to candidate date | I | Review queue only v1 |

### Status

| Scenario | Allowed | Output |
|---|---|---|
| Ship date set, status Open | `effective_status=shipped` | Rule + conflict flag |
| Invoice linked, ERP open | `effective_status=shipped_invoiced` | Rule |

### Links

| Scenario | Allowed | Min confidence to act |
|---|---|---|
| Job + invoice amount/date match | Suggest link | 0.85 auto |
| Customer name fuzzy match | Suggest link | 0.90 auto |
| Excel "4412" → ERP job | Normalize + match | 0.85 auto |

### Unstructured

| Input | Allowed output |
|---|---|
| Excel shortage table | Rows with SKU, qty, job_hiveflow_id candidate |
| PDF PO | Header fields only v1 — PO #, vendor, date |
| Email | Entity mentions → review queue |

---

## Human-in-the-loop requirements

| Decision | v1 default |
|---|---|
| Customer link tier C | Human confirm |
| Fulfillment–invoice link tier C | Human confirm |
| Schema map change suggested by AI | Internal human approve |
| New customer alias | Human confirm (or auto after 2nd identical confirm) |
| Tier I promise date in strict tenants | Human policy at onboarding |

Overrides are **first-class data** — stored in tenant memory, not throwaway feedback.

---

## Model usage patterns

### Prefer: small, validated steps

| Step | Approach |
|---|---|
| Column mapping suggest | LLM on header row + 5 sample rows → human approve |
| Match ranking | Embeddings + rules hybrid; explain top 3 signals |
| Unstructured extract | Layout-aware parse → LLM for ambiguous cells |
| User narrative | Template fill from provenance JSON; LLM optional polish with validator |

### Avoid: end-to-end "figure out the business"

Single prompt over raw CSV → summary is **not** production architecture.

---

## Validation layer (mandatory)

Every AI output passes validators before entering the graph:

```python
# Conceptual — not implementation
validate_mapping_suggestion(suggestion)  # known entity types, no new $ fields
validate_link_candidates(candidates)     # evidence list non-empty
validate_narrative(text, provenance)   # every $/date in text ∈ provenance
validate_extracted_rows(rows, schema)  # types, required keys
```

Failed validation → fallback to rules-only or review queue.

---

## Differentiation vs commodity "AI analytics"

| Commodity | HiveFlow AI |
|---|---|
| Chat with your data | Silent reconciliation |
| Generate dashboards | Rank exceptions from trusted joins |
| Forecast anything | Conservative gap-fill with flags |
| Single-system copilot | Cross-system + Excel + email |
| Black box answer | Provenance chain per fact |

**Sales never leads with this table.** It informs build and defensibility.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Wrong late job | Suppression rules; provenance; tuning period |
| Wrong unbilled $ | Require link tier B+; show calculation |
| Mapping drift after ERP update | Batch anomaly detection; playbook versioning |
| Over-reliance on LLM | Template-first narratives for v1 |
| Services creep ("fix our data") | Fit gate at sale; charge PS for cleanup |
| Customer fear of AI | Don't mention AI; say "we reconcile your systems" |

---

## Open research questions

- [ ] Embedding model vs rule-heavy fuzzy match for customer names at SMB scale
- [ ] When to auto-approve tier B links after tuning period ends
- [ ] Minimum viable unstructured ingest (Excel only vs email day one)
- [ ] LLM vendor vs self-hosted for PII-sensitive client data

---

## Related

- [reconciliation-engine.md](./reconciliation-engine.md)
- [confidence-and-provenance.md](./confidence-and-provenance.md)
- [v1-scope.md](./v1-scope.md)
