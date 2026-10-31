# cloudops_domain

Knowledge, assurance and reporting (step 17, ADR 0020). Deterministic domain logic over
the platform database, reached only through `cloudops_tenancy` connections it is handed.
It never calls a model: AI drafting is `cloudops_agents.drafting`, and it writes drafts
only.

| Module | What it holds |
| --- | --- |
| `knowledge` | Document kinds from name and folder, staleness against the live estate, design and handover gaps, citations |
| `assurance.library` | Control library validation; loading goes through `access.load_controls` |
| `assurance.evidence` | The evidence queries controls route to, their proposal rules, and the append-only store |
| `assurance.scoring` | Compliant 100, partial 50, non-compliant 0; manual excluded; approved results only |
| `assurance.pack` | The signed evidence pack: canonical JSON, SHA-256, Ed25519 |
| `reporting.sections` | The monthly pack's sections from live data; `FinancialSections` is the step 16 seam |
| `reporting.money` | Money rounded once, in its currency |
| `reporting.render`, `reporting.pdf` | The pack as lines, and as a PDF of exactly those lines |
| `reporting.portfolio` | Consolidation in a reporting currency, from the same rows |
| `reporting.acceptance` | The line-by-line diff against the manually assembled pack (`acceptance/README.md`) |

Tests: `services/domain/tests` (no database) and `db/tests/test_{knowledge,assurance,
reports,drafting}.py` (the real schema, isolation included).
