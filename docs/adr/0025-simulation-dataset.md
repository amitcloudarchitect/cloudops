# ADR 0025: An end-to-end simulation for one test customer

- **Status:** Proposed, 2026-10-31
- **Decided by:** Amit Kumar (to accept on merge)
- **Implements:** test data for the end-to-end run against the lab subscription.
  `packages/devdata/src/cloudops_devdata/simulation/`, `evals/golden/simulation.jsonl`,
  the evaluation harness's per-tenant knowledge, `docs/simulation/README.md`,
  `make simulation` and `make simulation-load`.

## Context

An end-to-end run needs one customer whose CIs are real Azure resources (the lab
subscription, onboarded read-only under ADR 0003) and whose tickets, findings and
commercials look like operations data. The operations workbook has the right
shapes. It also has real customer names, staff names and ticket text, none of
which may enter the repository.

## Decisions

1. **Distributions, not data.** The workbook was read once, outside the repo, for
   aggregates only. They are committed as `params.json`: numbers and generic
   vocabulary. All text is invented. The generator never reads the workbook. A
   test builds a denylist from the workbook when it is present and checks the
   output against it, and skips otherwise.
2. **The lab's VMs are read at run time, never committed** (user). The repository
   is public. The generator reads the portal's VM export from a local,
   gitignored file (`--estate`, default `build/simulation/estate.csv`), and so
   everything generated from it stays local too. Later the source becomes the
   resources discovered for the tenant. The open incidents are role templates
   bound to whatever estate is given. Tests, the golden set and the evaluation
   use an invented example estate with RFC 5737 and RFC 1918 addresses. A test
   run with the real export checks that none of its facts are in the changed
   files. Private addresses and OS versions are simulated, because the export
   has neither.
3. **Generated in `packages/devdata`**, beside the synthetic seed. It reuses that
   package's row types, its insert path through `packages/tenancy`, and its place
   on the monetary-literal allowlist. Output goes to `build/` (gitignored), not the
   repository: the same anchor and seed reproduce it byte for byte.
4. **The tenant is found by short code `AZSPONSOR`, never by a fixed id.** The
   database requires capitals (`access.onboard_tenant`). If the tenant exists,
   whoever onboarded it, its id is used. Otherwise the local administrator
   onboards it.
5. **Findings reach the platform only through the Qualys connector**, served the
   simulation's recorded responses. The loader writes none itself, so what is
   tested is the step 15 code.
6. **No per-customer rate card.** The platform's versioned rate card prices this
   customer like any other. Its own terms are on its SOW.
7. **The evaluation gains a fourth tenant** (`azsponsor`) holding the example estate's VMs. Its
   knowledge is passed to `run_cases` per tenant. The baseline moved with the new
   cases (21 to 50), with classification accuracy rising and nothing zero-tolerance
   changed.
8. **A dev-only seeder writes the records to the developer instance** (user,
   added scope). `tools/simulation/servicenow_load.py`:
   - uses a separate seeding account from the local secret store, never the
     platform's read-only client or `admin`;
   - only accepts `devNNNNNN.service-now.com` in the local profile;
   - is idempotent on the `SIM-AZSPONSOR` marker, and purges only marked
     records;
   - logs counts only.
   It is test-data tooling, not platform write-back. The Import Set files
   remain the fallback.

## Consequences

- Loading into ServiceNow is the seeder (once the seeding account exists) or a
  manual, reversible import in the instance UI. Both are described in the
  README.
- **Found while building:** triage does not read ServiceNow's `description` (it is
  kept in `raw_extra`). Instructions planted there do not reach the classifier
  today. Two such tickets are in the files. If triage starts reading
  descriptions, they become injection cases, and one is worded to slip past the
  heuristic.
- In dev, the `servicenow` alias "Azure Sponsorship" must be approved on the
  tenant through the mapping flow before the connector resolves the tickets.
