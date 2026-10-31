# ADR 0026: The execution plane in the dev lab

- **Status:** **Proposed**, 2026-10-31. Not accepted. Nothing in it may be built until
  the user accepts it and the security review is signed.
- **Decided by:** *(pending: the user)*
- **Review pack:** held privately by the owner. It is not in this repository, because
  the repository is public.
- **Implements, once accepted:** HLD M5 (credential broker and execution plane) and M6
  stages 4–10, for one dev test customer only.

## Context

CLAUDE.md forbids building the execution plane before a security review. The user wants
the platform to remediate incidents on a dev test customer according to the HLD action
classes, and to write back to ServiceNow. A security review pack covers:

- the threat model;
- the dev credential design;
- the first runbooks;
- a checklist that maps each HLD guardrail to its control and the test that proves it.

This ADR records the shape of the decision. The pack holds the detail.

## Proposed decisions

1. **A review comes first.** No execution code, action credential, action role or
   write account is created until this ADR is accepted and the review is signed.
   Recording the sign-off is the user's act.
2. **Phases, each gated by the previous phase's exit criteria:**
   - **Phase 0, foundation.** The action log, the approval state machine, signing, the
     transcript store, and a broker that can only refuse. Nothing acts.
   - **Phase 1, ServiceNow write-back.** Work notes, then resolve and close, only after
     a verification check passes. Uses a separate, field-scoped write account (HLD).
   - **Phase 2, class B runbooks** on the dev test customer: start or restart a VM,
     restart a service.
   - **Phase 3, classes C and D, always with approval:** disk extension, certificate
     rotation, and resize or patching in a maintenance window, with a change record.
   - **Class E is never agent-initiated** (HLD).
3. **Scope.** Dev, and the one test customer only. Production keeps the HLD and LLD
   design without exception: delegated access, and short-lived credentials that the
   broker mints. ADR 0003 is not widened by this ADR.
4. **Every control in the review's checklist has a named test.** A phase does not exit
   with a control untested.

## Decisions taken by the user (2026-10-31)

| Ref | Decision | The user's answer |
| --- | --- | --- |
| D1 | Autonomy ceiling | **Supervised first.** A class B runbook becomes unattended only after it has a documented record of clean supervised runs, and only when the user promotes it. The platform never promotes itself. C and D are always approved. E is never agent-initiated |
| D2 | Action classes consented to | **A and B only, for now.** That covers phases 1 and 2. Phase 3 (classes C and D) needs an amendment to the consent below before it starts |
| — | Dev credential | **A managed identity, federated to the executor's workload. No stored secret.** ADR 0003 stays read-only |
| — | Who signs | **The user, as lab owner, for dev.** A named Getronics security reviewer, other than the user, signs before UAT or any real customer |

## Still open

Where the execution plane runs in dev, ServiceNow close behaviour, transcript retention
and redaction (LLD L10), the signing ceremony (LLD L9), and separation of duties. All
are in the private pack, owned by the user.

## Consent (D2)

Given by the user (Amit Kumar, lab owner) in writing on 2026-10-31: **"Consent
given."** That statement answers the questions put to them that day, and on that basis
it covers:

- **Action classes A and B only**, which are phases 1 and 2. Classes C and D need an
  amendment to this consent first. Class E is never agent-initiated.
- **The dev test customer's subscription, which the user has declared a test estate.**
  Other users' resources in it are included. The platform's own resources are
  excluded.
- **No end date was stated.** The consent is withdrawn or narrowed by amending this
  ADR.

The security review sign-off below is a separate act and is still to be completed.

## Review sign-off

*(To be completed by the signer(s). Not by any Claude session.)*

| Role | Name | Date | Scope signed |
| --- | --- | --- | --- |
| Lab owner | | | Dev, phases 0–2 |
| Security reviewer (before UAT or any real customer) | | | |

## Consequences

- Phase C of the simulation can start with phase 0 only. Each later phase is its own PR
  that refers to this ADR.
- The review pack stays out of the public repository. Accepting this ADR records the
  decisions, and the pack stays with the owner.
