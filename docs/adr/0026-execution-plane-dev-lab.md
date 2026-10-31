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

## Open decisions

| Ref | Decision | Recommendation in the pack | Owner |
| --- | --- | --- | --- |
| D1 | Autonomy ceiling: may class B run unattended, and from when? | Supervised first; unattended per runbook only after a documented number of clean runs, promoted by the user. C and D always approved; E never | The user |
| D2 | Consent for agentic execution | For this lab, the user consents in writing in this ADR. The consent names the action classes and the resources it covers, and covers nothing beyond them | The user |
| — | The dev credential option, where the execution plane runs in dev, the target scope, ServiceNow close behaviour, transcript retention and redaction (LLD L10), the signing ceremony (LLD L9), separation of duties, and who signs the review | In the private pack | The user |

## Consent (D2)

*(To be written by the user.)*

## Review sign-off

*(To be completed by the signer(s). Not by any Claude session.)*

| Role | Name | Date | Scope signed |
| --- | --- | --- | --- |
| Lab owner | | | |
| Security reviewer | | | |

## Consequences

- Phase C of the simulation can start with phase 0 only. Each later phase is its own PR
  that refers to this ADR.
- The review pack stays out of the public repository. Accepting this ADR records the
  decisions, and the pack stays with the owner.
