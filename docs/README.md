# HiveFlow technical docs

Engineering architecture, data-model, and execution specs for the `hiveflow` codebase.

**North star:** one centralized, governed data model spanning every system a business runs on — consumed by reporting and by AI agents that optimize operations. The long-term shape is **industry-specific connectors + industry-specific data model frameworks**, so implementation for the next customer in an industry is fast. QBO, QBD, and BC are the horizontal finance foundation, not the destination. See [product-vision.md](./product-vision.md).

**DMaaS (Data Model as a Service)** is how that model is delivered: a cloud service that exposes the built, governed, continuously updated semantic model (dimensions, facts, relationships, metrics) through APIs so applications, BI tools, and AI agents can consume structured meaning without building the model themselves. Connect, DNA Engine, and Reporting Engine are capabilities inside DMaaS — see [architecture.md](./architecture.md#product-framing--dmaas).

| Document / folder | Purpose |
|---|---|
| [product-vision.md](./product-vision.md) | **Start here** — north star, layer model, connector tiers, vertical prioritization |
| [architecture.md](./architecture.md) | Current-state platform architecture + DMaaS framing |
| [kpi-generator.md](./kpi-generator.md) | KPI Generator portal workflow (draft → review → approve) |
| [spreadsheet-engine.md](./spreadsheet-engine.md) | Spreadsheet Engine — Excel upload, schema proposal, transforms, silver reference |
| [dbc-data-model.md](./dbc-data-model.md) | Business Central data model notes |
| [business-central-setup.md](./business-central-setup.md) | BC connector setup |
| [bc-source-documentation-lambdas.md](./bc-source-documentation-lambdas.md) | BC MS Learn source-docs Lambdas (scrape / relationships / tags) |
| [internal-execution-scoping/](./internal-execution-scoping/) | Lake, DNA engine, reconciliation, and related specs — several carry an earlier manufacturer/distributor ICP framing; engineering detail still valid, positioning superseded by [product-vision.md](./product-vision.md) |
| [onboarding/hive-flow-ai-domain.md](./onboarding/hive-flow-ai-domain.md) | Domain / DNS onboarding |

Connector operator guides live in [`../onboarding/`](../onboarding/).

Business, GTM, commercial, and product-catalog content lives in the sibling folder `../hiveflow-business/` (on disk today: `meshflow-business/` — rename pending). Never load it into engineering tasks.
