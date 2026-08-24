# HiveFlow monorepo

Installable packages live under `packages/`. Prefer opening a **single package folder** as the Cursor workspace to keep context small.

| Task | Open workspace |
|---|---|
| Config, paths, parquet I/O | `packages/hiveflow-platform` |
| QBO / QBD / BC ingest | `packages/hiveflow-connectors` |
| Silver / Glue catalog | `packages/hiveflow-lake` |
| DNA compile / governance / packs | `packages/hiveflow-dna` |
| Portal UI / Cognito / charts | `packages/hiveflow-portal` |
| CDK deploy / cross-package | **repo root** |

## Rules

- Do not load sibling `../hiveflow-business` (GTM, terms, product-scoping) into engineering tasks.
- `hiveflow.dna` must not import `hiveflow.dna.web`.
- Shared lake I/O: `hiveflow.storage.parquet` + `hiveflow.storage.paths`.
- Dev install: `.\scripts\install_dev.ps1` (editable install of all packages).
- Technical docs: `docs/` (product framing in `docs/product-vision.md`, deployed architecture in `docs/architecture.md`). Operator guides: `onboarding/`.
- Prefer changes that generalize into a reusable industry pack over ones that solve a single customer — see `docs/product-vision.md`.

See each package’s `AGENTS.md` for the default read set.
