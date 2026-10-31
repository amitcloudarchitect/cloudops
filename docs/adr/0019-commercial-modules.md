# ADR 0019: Commercial modules — FinOps, pricing and margin

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Implements:** step 16; HLD M10 (FinOps), M11 (effort and pricing), M12 (margin),
  Multi-currency, principles 1 and 7; LLD Row-level security, Multi-currency, CI/CD
  (pricing regression, monetary literals), open item L4. Migration `0012_commercial`,
  `packages/commercial`, `connectors/src/cloudops_connectors/finops`, the Azure cost
  export, provider-total, invoice and ECB connectors, `services/api/.../commercial.py`,
  and the console's FinOps, Margin and Pricing screens.

## Context

The platform is the sole master of cost reconciliation, margin, SOW commercials, people
cost, FX and the pricing model (HLD). CoreStack reports cost but holds no SOW, so the
platform ingests the providers' billing exports itself. A figure that cannot be
reconciled is not shown as if it were; a gap shows as "not captured", never zero.
Step 15 already loaded AWS CUR into `cost_fact` with a first charge classification and
atomic restatement per assembly; `fx_rate` and `to_currency()` existed since step 03;
`sow` had its restrictive policy. The LLD leaves the FX source (L4) and several
commercial rules open; the user decided them before the build.

## Decisions

1. **FX source (L4): ECB reference rates, with a Finance correction path** (user).
   The ECB connector stores every directed pair: the daily rate as `average` effective
   that day (flows convert at the rate in force on the transaction date) and the last
   business day's as `closing`; crosses are derived, both directions stored (there is
   no inverse fallback). A monthly average is the mean of the month's daily rates,
   computed where it is used. A correction goes through `access.correct_fx_rate`
   (permission `fx.manage`: CFO, FinOps, platform administrator), which changes the
   rate in force and writes the explicit entry in `fx_restatement` in one transaction;
   an ECB revision writes the same entry. A finance correction is never overwritten by
   the feed. Closed months it touches are restated by month-end reprocessing.

2. **Reconciliation compares with the provider's own figures** (user). Daily: each
   account's month-to-date line items (unblended, as billed), summed over exactly the
   days the provider's total covers, against that total (AWS Cost Explorer, Azure Cost
   Management query). At close: against the issued invoice (AWS invoice summaries,
   Azure invoices), which then decides what is shown. Drift above 0.5% marks the
   figure unverified and raises a P2 alert once per account, period, kind and day; a
   currency mismatch is unverified too. No reference yet is unverified without paging.
   A customer's figure is verified only when every account with cost is. Endpoint
   shapes are recorded fixtures until credentials exist (ADR 0003, 0016).

3. **Margin target and banding** (user). The target is per SOW (`sow.target_margin`).
   Bands are the reference engine's: below 10% critical, below 20% at risk, below 30%
   watch, else healthy; they live in the rate card. Gap = margin − target.

4. **Actual effort is ServiceNow's `actual_hours` only** (user). Work items closed in
   the month without hours are counted; while any are missing, the actual figure is
   partial and the band falls back to the modelled cost, and says so. Nothing is
   estimated from durations.

5. **FX impact against the previous month's rates** (user). The same month's margin
   computed at last month's average rates; FX impact is the difference, covering both
   cost-to-contract (EUR people cost into the contract currency) and contract-to-
   reporting conversion. The snapshot stores the margin at both rate sets in the
   contract currency; the API converts each at its own month's rate.

6. **Anomaly thresholds are data** (user). Platform default in
   `staging.anomaly_threshold_default`, per-tenant override in
   `cloudops.anomaly_threshold`, each with its currency (converted into the account's
   currency at the rate in force). Day over day: a day against the mean of the seven
   before it, above +30% and 100 EUR. Month over month: the month-to-date run rate
   against last month, above +20% and 1,000 EUR. Both directions recorded; spikes
   alert; a recomputed day that no longer crosses (billing restated) is removed.

7. **The pricing engine is a port, wrapped in a versioned rate card.** `pricing.py`
   keeps the reference's functions, names and order of operations, in `Decimal`; the
   constants moved digit for digit into `ratecard.py`, the one file the monetary-
   literal gate allows them in. A rate card version is immutable and effective-dated;
   a quote and a margin snapshot name the version they used. The CI job
   `pricing-regression` runs the reference engine and then the port's gate, which
   requires equality at six places on the reference case and across every coverage,
   support model, location and a spread of inventories (ADR 0013's open item closed).

8. **Margin is computed in the contract currency, by a worker, and versioned.** The
   FinOps worker (Temporal `FinOpsDaily`: reconcile, anomalies, margin) runs on the
   ingest login under the `finops` role class, so the restrictive policy admits it,
   and lists customers through `cloudops.finops_tenants()` (resolver-owned, ingest
   only). The current month is provisional (month to date, `through`); from the 5th
   the previous month closes; closed months are reprocessed for three months and a
   change writes a new version naming the one it restates — never an overwrite.
   Modelled cost quotes the customer's priced critical CIs and the month's
   consumption-class cost (only consumption feeds margin; licence, marketplace,
   support, reservations, savings plans, tax and credits are shown apart, HLD D8).

9. **Two locks on commercial data.** `margin_snapshot` and `margin_variance` carry
   `tenant_isolation`, the restrictive `commercial_visibility`, and an all-accounts-only
   account policy (margin is per customer, never per subscription). The API requires
   `commercial.read`. The isolation suite proves an engineer assigned to a customer
   reads its tickets and not its margin, with the database refusing even if the
   matrix were wrong. Reconciliation, anomalies and billing totals are cost data:
   tenant- and account-scoped, `cost.read`.

## Decided here, for review

- **Catalogue mapping.** `type_canonical` → catalogue row is in the rate card
  (`catalog_type_of`); types with no row (storage accounts, disks) are *unpriced CIs*,
  counted and shown. Commercial should confirm the mapping.
- **Mixed delivery has no people cost.** The workbook prices India and Budapest only;
  a `Mixed` tenant's margin is not captured (`no_people_cost`) rather than an invented
  blend. Fabrikam is Mixed in the synthetic data.
- **Months without billing data are not captured** (`no_cost_data`): the base pack and
  governance price on consumption, so a modelled cost without it would be wrong.
- **Quotes price in the rate card's currency (EUR) only.** Converting a forward quote
  is a commercial decision; the endpoint refuses another currency.
- **Variance attribution is proportional.** Hours above the priced slab (implied FTE
  × working days × 8) are attributed to each workgroup and activity type by its share
  of the month's observed hours.

## Consequences

- HLD D6 (the 24x7 multiplier) and D7 (Budapest people cost) stay open; the rate card
  carries the workbook's values until commercial and Finance confirm them.
- The FinOps worker needs a deployment (image exists: the connectors image; Kubernetes
  manifests and the Temporal schedule are a follow-up, outside `infra/gitops` in this
  change because the console deployment is in progress there).
- Alerts are structured log events (`finops.alert`); the Log Analytics alert rule that
  pages on them is step 19 (observability).
- Margin needs SOW capture (HLD dependency): until then a customer shows not captured.

## Amendment: review follow-ups (migration 0013)

Decided with the user after the step 16 review:

- **Margin records whether its cost reconciled** (`margin_snapshot.cost_verified`),
  stored rather than looked up at read time, so a closed month keeps what was true when
  it closed. True when every account with consumption in the month has a latest check
  that is verified (the invoice once there is one, else the provider total); false
  otherwise, including an account never checked; null with no cost. A change restates
  a closed month as a new version. The console marks "Cost not reconciled". The first
  run after 0013 restates closed months in the reprocessing window once, because their
  stored flag is null.
- **An FX correction moves its inverse and crosses.** Rates are stored for every
  directed pair. Correcting A → B also sets B → A = 1 / rate and, holding the legs from
  A fixed, B → X and X → B for every stored A → X on that day and rate type. Each moved
  pair gets its own `fx_restatement` entry naming the correction, with source `finance`
  so the ECB feed does not undo it.
- **A provisional month carries figures but no band and no gap to target.** Full-month
  revenue against month-to-date cost would band too well, and nothing is projected to
  make up the difference. The band arrives at close.
- From review: a month with no work items or no hours is incomplete (modelled basis);
  invoice references sum every document in the period; one customer's margin failure
  stops only that customer.
