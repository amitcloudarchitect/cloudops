# ADR 0019: Reporting, knowledge and assurance

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar (the library and the acceptance pack); the rest is step 17
  carried out on this design
- **Implements:** step 17; HLD modules M14, M15, M19 and M20; LLD Vector store,
  Evaluation, Multi-currency. Migration `0012_reporting_knowledge_assurance`,
  `services/domain` (`cloudops_domain`), `cloudops_api.{knowledge,assurance,reports}`,
  `cloudops_agents.drafting`, `cloudops_evals.drafting`, the gateway's
  `KnowledgeIndexer`, and the console's Knowledge, Assurance and Reports screens.

## Context

The step asks for three modules on top of what steps 05 to 15 ingest: a knowledge
layer, an audit engine and a monthly reporting pack. Two of its inputs did not exist
in the repository when the step began, and the user decided how to proceed without
them:

- **The 98-control library** is reference data Security supplies (LLD,
  Pre-deployment readiness). The engine is built; the library is not invented. Tests
  and the golden set use clearly fake controls (`TEST-*`, `EVAL-*`, `E2E-*`).
- **The manually assembled pack** for the acceptance diff. The diff harness is built
  and tested; the acceptance itself is pending until a real pack is supplied
  (`services/domain/acceptance/README.md`).

Step 16 (FinOps, pricing, margin) was being built in parallel and was not on main.

## Decisions

1. **A domain package, `services/domain` (`cloudops_domain`).** Deterministic logic
   for all three modules, reaching the database only through `cloudops_tenancy`
   connections it is handed. It never calls a model, so the API can import it without
   the gateway or the agents. AI drafting lives in `cloudops_agents.drafting` and
   writes only `draft_*` columns.

2. **Knowledge.**
   - **Tenant scope at ingest.** SharePoint chunks are written with the site's tenant,
     as before, and now with their `document_id`, `ordinal` and `page`. A document's
     `doc_kind` (design, handover, policy, runbook, report, other) comes from its file
     name, then its folder, never from its text (rule 4).
   - **Embedding** is done by `KnowledgeIndexer` in the AI worker
     (`KnowledgeIndexWorkflow`, every 15 minutes), one tenant's scope at a time, as
     the application login. It fills only an embedding that is still empty, of a body
     unchanged since it was read. The same sweep turns closed tickets' close notes
     into chunks. The connector process has no gateway and no model configuration, and
     keeping it that way kept deployment config out of this step. The tenant list
     comes from `cloudops.knowledge_tenants()`, ids only, owned by
     `cloudops_resolver`.
   - **Staleness** (superseded by the live estate) is a query, not a stored verdict:
     a document names a resource removed since it was modified, or the estate has
     grown 20% since, or it is a year old. Resources found by the first discovery are
     the baseline, not growth. **The thresholds are proposals** for the product owner
     (`cloudops_domain.knowledge`).
   - **Gaps:** customers in scope with no current design document or handover pack.
   - **Citations** name the document, link and page. Text files have pages only where
     form feeds mark them; real page numbers arrive with Office and PDF extraction,
     which the connector does not do yet.
   - The knowledge screens are staff-only for now: what a customer sees is step 18's
     decision.

3. **Assurance.**
   - **The library** (`cloudops.control`) is reference data with no tenant. It is
     written only by `access.load_controls`, for a principal holding
     `assurance.library`, after `ControlLibrary` validates the file: the declared
     count must match (a truncated file is refused whole), each automated control
     names a registered query that asks the system it declares, and manual controls
     are answered by a person. A reload retires missing controls; it never deletes.
   - **Evidence routing:** `cloudops_domain.assurance.evidence.QUERIES` holds six
     fixed queries (Qualys, Entra, Azure and AWS tags, ServiceNow SLA, SharePoint
     documents, CoreStack). Their thresholds come from the library's parameters,
     never from code. A query *proposes* a result; it never sets one.
   - **The evidence store** (`cloudops.evidence`) keeps the query text, parameters,
     raw value, retrieval time and a digest. It is append-only by trigger, for the
     owner too.
   - **Scoring:** 100, 50 and 0; manual and not applicable are excluded; only
     approved results count; with nothing approved there is no score, never zero.
   - **No unaided compliance.** The drafter's output has no result field. A result
     exists only with an approval (a check constraint), and a trigger refuses any
     approver who is not an active staff principal holding `assurance.approve`.
     "Compliant" also needs a narrative and either collected evidence or a citation.
     Editing an approved narrative withdraws the approval.
   - **The signed evidence pack** is canonical JSON (the assessment, every response
     and approval, every evidence row, the score), signed over its SHA-256 with
     Ed25519, and recorded append-only in `cloudops.evidence_pack`.

4. **Reporting.**
   - **Generation** runs in the API from live rows, under the caller's scope narrowed
     to one tenant, and stores the snapshot (`report_pack.sections`), versioned per
     month. Sections follow the step's list. The **financial dashboard and revenue
     mix** come through `FinancialSections`, the step 16 seam; until it is wired,
     `NotYetAvailable` returns them as "not captured". **CSAT** is not captured:
     there is no survey source. **Availability** comes from the telemetry store
     locally, and is not captured in Azure while LLD L1 is open.
   - **Currency:** a pack is in the contract currency of the SOW in force for the
     month. Generating one therefore needs commercial visibility, and with no SOW
     nothing is generated. Billed consumption converts each cost row at the average
     rate on its own usage date, and a missing rate is a gap that names the pair and
     date. The **portfolio** is in a reporting currency the viewer picks, converted
     from the same rows, never from pack totals. Money is rounded once, at
     presentation (`reporting.money.present`, `EUR 1,234.56`).
   - **Never auto-published.** The narrator drafts the captured summary sections from
     figures and tables only (no ticket or document text). A draft is never printed.
     Approval and publication need staff holding `reports.publish`, and every
     narrative needs a person's text. Both are enforced by the API and by a trigger.
     Customers see published packs only: a restrictive policy bound to the
     application login and to `cloudops_customer` (ADR 0018). That role is granted
     nothing here, so customer scopes fail closed. The portal decides which report
     columns customers read, and grants them along with `reports.read`.
   - **PDF** is written by a small dependency-free writer (standard fonts, WinAnsi, so
     € and £ print), deterministically. `GET /v1/reports/packs/{id}/text` serves the
     same lines as text, and that is the acceptance diff's input.

5. **Drafting runs as a sweep.** The API records a request (`draft_state =
   'requested'`). `DraftSweepWorkflow`, every minute, lists requests through
   `cloudops.pending_drafts()` (id and tenant only, owned by `cloudops_resolver`) and
   drafts each one as an activity under that one tenant's scope. A failed draft is
   released back to the queue and never blocks the others. Deterministic checks run
   before and after the model:
   - instruction-like material is not drafted at all;
   - citations must be refs the drafter was given;
   - a narrative may not assert a result;
   - every number in a report narrative must appear in its data.

   A declined draft records why (`draft_note`). `python -m cloudops_agents.worker
   schedule` creates both schedules.

6. **A second golden set**, `evals/drafting/`, runs the drafting activities with
   replayed replies. Groundedness is scored on the raw output. Injection followed,
   cross-tenant leaks and results set without a person are zero-tolerance gates.

7. **Default permissions**, granted by system role name, because role classes are
   shared (Customer master is a `delivery_lead` class). They are refined in the
   console like every default:

   | Permission | Granted to |
   | --- | --- |
   | `reports.read` | CEO, CFO, Delivery lead, Commercial, Auditor, Platform administrator |
   | `reports.manage`, `reports.publish` | Delivery lead, Platform administrator |
   | `assurance.read` | CEO, SecOps, Delivery lead, Auditor, Platform administrator |
   | `assurance.manage`, `assurance.approve` | SecOps, Delivery lead, Platform administrator |
   | `assurance.library` | Platform administrator |

## Open, and not decided here

- **The evidence-signing key.** The LLD names no such key; its Key Vault Premium keys
  are runbook, transcript, database and token signing. Locally the API signs with an
  Ed25519 seed from the local secrets file (`EVIDENCE_SIGNING_SEED`). Every other
  environment refuses to export (503 `signing_unavailable`) rather than sign with
  nothing. The options are:
  - a new HSM-backed key `key-evidence-signing` in `kv-cloudops-keys`, signing inside
    Key Vault (recommended);
  - reuse `key-runbook-signing`, which mixes two trust purposes.

  Added to the LLD as open item L13.
- **The control library and the manual pack** (see Context).
- **Wiring step 16** into `FinancialSections` once it is on main.

## Consequences

- Six new tenant tables, each FORCE RLS with `tenant_isolation`, an all-accounts
  restriction (assessments and packs are tenant-wide), and isolation tests. One
  non-tenant reference table, `cloudops.control`, readable by the application and
  writable by no application role.
- `knowledge_chunk` gains citation and embedding-state columns, and `document` gains
  its kind and folder: both expand-only.
- Two more resolver-owned functions and two `draft_queue_read` policies, confined to
  `cloudops_resolver`, join the catalog test's known list.
- The report-pack acceptance stays pending until a real pack is supplied.
- **Numbering:** step 16 was also renumbering to migration 0012 and ADR 0019 when
  this merged; whichever lands second renumbers.
