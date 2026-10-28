# ADR 0006: Triage (pipeline stages 1 to 3) and the evaluation gate

- **Status:** Accepted, 2026-10-28
- **Decided by:** Amit Kumar (step 08 carried out on this design)
- **Implements:** HLD, Agentic remediation and safety (Pipeline stages 1 to 3, Action
  classes, Guardrails); LLD, Evaluation. Code in `packages/agents` (classifier,
  enricher, `triage.py`, `remediation.py`), `packages/evals` and `evals/`.

## Decisions

1. **What the model says about a customer never decides scope.**
   - The tenant is known server-side: the connector resolved it from the ticket's
     company, and it is the workflow's input.
   - The classifier's `customer` is what the ticket text names, and is used only as a
     cross-check: a ticket naming a different customer goes to a human.
   - Its `affected_ci` is likewise text. The platform resolves it to a resource within
     the tenant, or to nothing.
2. **Routing is the model's output plus deterministic guardrails** (`triage.py`, pure
   functions used by both the workflow and the harness). Everything except one path
   leads to a human:

   | Stage | To a human when |
   | --- | --- |
   | 1 Intake | not an open incident or service request |
   | 2 Classify | below the confidence threshold; class E ("never agent-initiated"); instruction-like content in the ticket; another customer named; a CI not in the ticket text |
   | 3 Enrich | below the threshold; the resource is not found; the bundle cites anything not in the tool results it saw |

3. **An instruction-like-content heuristic in front of the model.** It is a regex over
   the *input* fields, never over model output, and deliberately broad: a false
   positive costs an engineer a glance. It does not replace the schema or the model's
   own resistance; `inj-005` exists for what it cannot catch.
4. **The classifier has no tools; the enricher has three read-only ones**
   (`get_resource_context`, `search_tickets`, `search_knowledge`). Neither can reach
   a write tool: none is on their allowlists, and the registry refuses writes anyway.
5. **The pipeline runs as `service:triage`**, with the engineer role class and three
   read permissions, scoped to the ticket's one tenant.
6. **The evaluation gate scores the whole stage 1 to 3 decision**, because that
   decision is what stands between a ticket and planning.
   - The model is replayed from cassettes, so CI is deterministic.
   - Groundedness is computed from the raw outputs, never from the guardrail that
     acts on it, so removing a guardrail cannot improve a metric.
   - Injection-followed and cross-tenant leaks fail at any value above zero.
7. **Golden cases from production corrections arrive as redacted candidates.** A person
   reviews and pseudonymises each one before CI counts it.

## Consequences

- Stages 4 onward are not built. Planning, policy, approval and execution need the
  security review first (CLAUDE.md), and nothing in this step can act on a customer
  environment.
- **The cassettes are authored until LLD L12 gives the tiers real models.** Until then
  the gate measures the pipeline, not model quality. Re-recording with
  `RecordingProvider` changes that without changing the harness.
- **The baseline is the reviewed record of quality.** It moves only by an explicit
  `make eval-baseline` and a commit, never by drift.
- Prompt changes are new files (`v2.md`), so a cassette set and a baseline always
  belong to a known prompt version.
