# ADR 0036: The SecOps workspace

- **Status:** Proposed, 2026-11-01 (accepted on merge)
- **Decided by:** the coordinating session's brief (staff workspace, narrowing, nav
  placement); decisions 2, 3 and 5 are modelled and open to the user's revision
- **Implements:** HLD M13 ("Qualys ingest, ageing against SLA, scanner coverage, posture
  score"). `services/api/src/cloudops_api/secops.py`; migration
  `0034_secops_quarantine_read`; `apps/web/src/screens/SecOps.tsx`, `SecOpsFinding.tsx`,
  `src/app/secops/`; the `secops-all` demo principal.
- **Builds on:** ADR 0018 (the customer's `/posture` view), ADR 0028 (the simulated
  Qualys source in dev), ADR 0030 (modelled SLA targets, marked as such).

## Context

Staff had no security view of their own. The customer's security officer has
`/posture` (ADR 0018): open findings by severity and age for one tenant. HLD M13 asks
for more: ageing against SLA, scanner coverage and a posture score, across the
customers a member of staff works on.

**The HLD and LLD set no vulnerability remediation target.** The LLD names the ageing
view and its indexes; the HLD says "age against SLA". Neither gives a number of days,
and no SOW in the platform carries one. As ADR 0030 did for ticket SLA, this ADR
models the targets and marks every figure that depends on them.

## Decisions

1. **A staff-only workspace at `/secops`, on `vulnerabilities.read`.** Four endpoints
   under `/v1/secops`: `vulnerabilities`, `coverage`, `posture-score` and
   `findings/{id}`. A customer is refused all of them with 403 `forbidden`, even
   though the customer roles hold `vulnerabilities.read`. A customer keeps `/posture`,
   which is unchanged; the age bands it uses are now shared with this module rather
   than copied (`portal.AGE_BANDS`, `AGE_BAND`). Queries run under the caller's
   resolved scope. On the three lists the customer selector's `tenant_id` narrows it
   through `ScopeDep` (ADR 0032), never widens it: a tenant outside scope is 404. The
   drill-down is reached by a finding's id, so it reads the whole resolved scope.
2. **Modelled remediation targets** by Qualys severity, counted from first detection:

   | Severity | Target |
   |---|---|
   | 5 Urgent | 15 days |
   | 4 Critical | 30 days |
   | 3 Serious | 90 days |
   | 2 Medium | 180 days |
   | 1 Minimal | none |

   - **Breached:** more than the target has passed. Exactly the target is not yet
     breached.
   - **At risk:** at least 75% of the target has passed (ADR 0030's fraction).
   - **Within:** less than that. **No target:** severity 1, however old.
   - Wall-clock time. A remediated finding is aged to its fix, not to now.
   - The policy is `cloudops_api.secops.SLA_TARGET_DAYS` and `AT_RISK_FRACTION`.
     Every figure derived from it carries `is_modelled` and the source
     `cloudops_policy`. A severity with no target shows "—", never zero.
   - A contractual target per customer (a SOW field) would replace this model.
3. **Scanner coverage** counts resources of type `compute.vm` (`SCANNABLE_TYPES`). Each
   is in one of three states:
   - **detected:** a Qualys finding is linked to it;
   - **asset id only:** its inventory record carries a `qualys_asset_id`, but no
     finding is linked;
   - **no match:** neither.

   Coverage is detected plus asset id only, as a share of scannable resources. With
   nothing scannable it is "—", not 0%.

   The connector reads host detections. A host that Qualys scanned clean therefore
   looks the same as one it never scanned. That is why a recorded asset id counts as
   matched, and why the two are reported apart.

   The other side is the **resource quarantine**: Qualys hosts that matched no
   resource. Matching follows the LLD: external key, then `name_norm` plus an IP, then
   `name_norm` alone. Each unmatched resource and each quarantined host shows how many
   counterparts share its `name_norm` or an address. That number is a hint for a
   person to confirm, never a link: nothing here writes a match.
4. **Staff read the resource quarantine** (migration 0034):
   - `cloudops_app` gets `USAGE` on `staging` and column `SELECT` on
     `staging.resource_quarantine`, without `raw_ref` (a blob path);
   - nothing is granted to `cloudops_customer`;
   - rows stay behind the table's FORCEd `tenant_isolation` and restrictive
     `account_scope` policies. A Qualys host carries no account, so a
     subscription-limited scope sees none.

   The isolation tests are in `db/tests/test_api_secops.py`.
5. **A modelled posture score per customer, 0 to 100.** It is the weighted mean of the
   parts that can be computed, rescaled to their weights:

   | Part | Weight | Value |
   |---|---|---|
   | SLA compliance | 50% | open findings with a target that have not passed it, as a percentage of open findings with a target (100 when there are none) |
   | Coverage | 25% | decision 3 |
   | Exposure | 25% | 100 less the average points per host Qualys has reported, floored at 0. Points per open finding: 10 at severity 5, 5 at 4, 2 at 3, 1 at 2, 0 at 1 |

   - SLA compliance and exposure need at least one host Qualys has reported a finding
     on.
   - With no part computable, the score is "No scanner data", never 0.
   - The score, its parts and its weights are `is_modelled`.
   - The response carries the formula in words, and the screen shows it with the
     Modelled mark. It is the platform's model: not a Qualys figure and not a
     contract measure.
6. **The drill-down.** `/secops/findings/{id}` shows the finding, its band and target,
   the matched resource with a link to its context page, and its tickets. Tickets are
   the work items linked to that resource: open ones, and any opened in the last 90
   days.
   - A finding on an unmatched host has no resource. Its tickets are `null`, meaning
     "nothing to link through", which is not the same as `[]`.
   - Another tenant's finding is 404, exactly like a missing one (LLD isolation
     case 1).
   - Threat and solution text from the scanner is shown as text, never followed
     (CLAUDE.md rule 4).
7. **Reachable by the seeded `secops-all` principal** (the default SecOps role, every
   tenant), so the journeys cover the role the workspace is for. The navigation entry
   sits in the Security area beside Security posture, staff only (ADR 0032 reserved
   it; this change shows it).

## Consequences

- Tests:
  - `db/tests/test_api_secops.py` (own database, fixed clock). It covers every band at
    its boundary (exactly the target, one second past it, exactly 75%, one second
    short of it), coverage states, quarantine hints, the score and its rescaling,
    the drill-down, cross-tenant 404s, and refusals to a customer and to a role
    without the permission. It also checks the customer's posture figures are
    unchanged, and the quarantine grant's isolation.
  - `apps/web/e2e/secops.spec.ts`: a journey each for SecOps, Engineer and Platform
    administrator; the customer selector narrowing every page; refusals for FinOps
    and for a customer security officer (no nav entry, 403 from the API); axe in
    both themes, and tablet width. The admin and engineer navigation lists gain
    SecOps.
- Qualys data in dev is the simulated estate (ADR 0028). It arrives after the
  connector's first daily sync, so the workspace is empty until then.
- The vulnerability table has no index on `resource_id`. Coverage reads the linked
  resource ids in one pass, not per resource. If that scan grows slow, the index
  belongs on each partition, which is a separate migration.

## Not decided here

- **Contractual targets per customer or SOW**, which would replace decision 2.
- **Scan-state ingest** (the Qualys host list and its last scan dates). It would tell a
  host scanned clean from one never scanned, and replace the asset-id rule in
  decision 3.
- **Mapping to controls** (HLD Qualys row), which belongs with assurance (ADR 0020).
- **Acting on a quarantined host** (creating or correcting the resource) from this
  screen. Today that is fixed at the source.
