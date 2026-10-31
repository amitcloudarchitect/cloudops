# ADR 0021: Customers see a price for cloud consumption, never Getronics' cost

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Amends:** ADR 0018 (the customer role's grant on `cost_fact`)
- **Implements:** ADR 0002 D10 (commercial data never reaches a customer role); CLAUDE.md
  rule 5 (money). Migrations `0014_customer_price` (expand) and `0015_revoke_customer_cost`
  (contract), `0016_pack_customer_price`, `services/api/.../bff.py` (`/v1/cost/summary`), `operations.py`
  (`/v1/customers`), the console's customer overview, the synthetic SOWs.

## Context

Migration 0011 granted `cloudops_customer` SELECT on `cost_fact.unblended_cost` and
`amortized_cost`, on the assumption that they were the customer's bill. The customer
roles CXO, VP or director, and Architect hold `cost.read`, and `/v1/cost/summary` and
`/v1/customers` read those columns for them.

On 2026-10-31 the user confirmed that `unblended_cost` is **what the cloud provider
charges Getronics**. That is Getronics' cost base, and a customer who knows it can infer
margin. This breached D10. No customer was onboarded on any environment, so nothing leaked.

The user chose this outcome: a customer sees its consumption at the price Getronics
charges, computed server-side, and the raw cost columns are revoked from
`cloudops_customer`.

**No pricing rule existed.** The SOW carries revenue and ACV figures. The rate card's
consumption-linked rates (base-pack bands, coverage uplift, CoreStack's 2.1%) price the
managed service, not the consumption itself. The billing connectors ingest no list price.
HLD lists chargeback invoice generation as out of scope. So the rule was asked for, not
inferred.

## Decisions

1. **The pricing rule** (user, 2026-10-31):

   - **Price = amortized cost × (1 + the SOW's consumption uplift)**, per usage date, in
     the transaction currency of the cost line.
   - **The uplift is per SOW**: `sow.consumption_uplift`, a fraction (`0.08` is 8%),
     `numeric(7,6)`, `0 ≤ uplift < 10`. The SOW in force on the usage date
     (`effective_from ≤ day ≤ effective_to`, inclusive, as the FinOps worker reads it)
     supplies it, so a renewal that changes the rate prices each day at its own rate.
   - **It applies to every charge class**: consumption, licence, marketplace, support,
     reservation, savings plan, tax and credit alike.
   - **The basis is amortized cost**, so reservations and savings plans are spread over
     the days that use them. The provider's unblended figure is not used.
   - **A SOW without an uplift is not captured.** No price is shown, and the gap is visible
     (HLD principle 7). The cost is never used as a fallback, and a missing uplift is never
     treated as zero.
   - Never guess: no SOW in force, or two overlapping, is also not captured.

2. **Computed in the database, by a function only customers can call.**
   `cloudops.customer_price(from, to)` returns rows of tenant, account, usage date, charge
   class, currency and price, unrounded. A NULL price means not captured.

   - It is SECURITY DEFINER, owned by `cloudops_resolver`, and EXECUTE is granted to
     `cloudops_customer` only. **Staff never get it**: an engineer holds `cost.read`, so
     the price divided by the cost would be the uplift, which is commercial. The
     isolation suite tries every staff role and expects `permission denied`.
   - Row-level security still scopes the definer. `tenant_isolation` and `account_scope` on
     `cost_fact` apply to every role and read the caller's `app.*` settings. The function
     therefore returns the caller's tenants and subscriptions only. A subscription-limited
     customer is priced on its own subscription, and a test proves it.
   - `commercial_visibility` on `sow` additionally admits `current_user =
     'cloudops_resolver'`. That is the definer, and only inside its own functions. The
     resolver gets SELECT on four SOW columns (tenant, the two dates, the uplift), never
     revenue, and on six `cost_fact` columns.
   - Why not a worker-written price table: the FinOps worker is not deployed yet
     (ADR 0019), so a table would show nothing in dev. A table would also go stale when an
     uplift changes, while the function is always current.

3. **The cost columns are revoked**: migration 0015, `REVOKE SELECT (unblended_cost,
   amortized_cost) ON cloudops.cost_fact FROM cloudops_customer` (and
   `cost_finding.estimated_saving`, decision 6). A customer scope naming
   either column, including through `SELECT *` or `ORDER BY`, gets `permission denied`.
   It still reads the line items' dates, services, charge classes and currency. The SOW
   uplift is refused to it as well, since `cloudops_customer` holds no grant on `sow`.

4. **A recorded exception to expand-then-contract.** The LLD and 0003 say a contract
   migration ships a release after its expand. 0015 ships in the same release as 0014 and
   the API change. The previous release's use of these columns for customers *is* the
   defect, so revoking at once fails closed. A customer's cost card on an old pod errors
   during the canary instead of leaking, and no customer is onboarded. Note for whoever
   tightens the gate next: `test_migration_policy` does not catch a REVOKE on a table,
   and 0009 revoked privileges from `cloudops_app` inside an expand. 0015 declares
   `contract` because that is what it is.

5. **Serving** (CLAUDE.md rule 5):

   - **`/v1/cost/summary`** returns `PriceSummary` to a customer: per provider and
     currency, `price` (a `Metric`, source `customer_price`) or `not_captured:
     "no_uplift"`. A line with any unpriced day is not captured as a whole, so a
     partially priced sum is never shown. Staff still get `CostSummary` with `amortized`.
   - **Conversion.** Given `currency`, both views also return `converted`, at the daily
     average rate in force on each usage date. A line with any day lacking a rate has no
     conversion, never a rate of 1 or 0.
   - **`/v1/customers`** returns `price_30d` to a customer and `cost_30d` to staff. Each
     is null for the other kind.
   - **Rounding.** Nothing is rounded server-side. The console rounds once, in `money()`.
   - **Console.** The customer overview shows "Cloud consumption, 30 days (EUR)" with the
     price, or "Not yet priced" when not captured. Staff still see "Cloud cost".

6. **A finding's saving is priced the same way** (user, 2026-10-31). CoreStack's
   `cost_finding.estimated_saving` is stated in provider-cost terms, so it could reveal
   part of Getronics' cost base. 0015 revokes it from `cloudops_customer`, and
   `cloudops.customer_saving()` (0014, same ownership, grants and RLS scoping as the
   price function) returns each finding's saving × (1 + uplift) in `saving_currency`.
   A saving is spend avoided from now on, so the uplift is the one in force **today**,
   not on the detection date. The not-captured rules are the same. No API or console
   screen shows findings yet; one that does reads the saving through this function.

7. **Synthetic data.** Northwind 8%, Contoso 6%, Fabrikam 10% and Tailspin 7% (both of
   its SOWs). Woodgrove has no SOW, so its customers would see "Not yet priced".

8. **Report packs take the price at generation (user decision, 2026-10-31; migration
   `0016_pack_customer_price`).** Packs are generated under a staff scope, which cannot
   call `customer_price`. The alternative, pricing when a customer opens a published pack,
   was rejected: a published pack would change silently when cost is restated or an uplift
   edited, and the approver would never see the customer's figure.
   - `cloudops.pack_customer_price(tenant, from, to)` wraps `customer_price` for one tenant
     and returns one price per usage date and transaction currency. A day with any
     unpriced line is NULL for the whole day. It is SECURITY DEFINER, owned by
     `cloudops_resolver`, and executable by `cloudops_app` only. It raises unless
     `app.role` is one of the roles `commercial_visibility` admits. Those roles may read
     the SOW, so seeing price beside cost tells them nothing new. Every role holding
     `reports.manage` is one of them. `cloudops_customer` gets no grant.
   - The executive summary carries "Cloud consumption" (`customer_price`) in the contract
     currency. Each day is converted at the average rate on its usage date, and nothing is
     rounded before presentation. Any unpriced day or missing rate makes the whole figure
     a named gap, never a partial total and never the cost.
   - **Where each figure is stored, enforced by row-level security.** `reports.read` and
     `cost.read` are both non-commercial, so an administrator may grant both to an
     Engineer-class role. With the price in `report_pack.sections`, which every
     `reports.read` holder reads, that role could divide price by cost and read the uplift.
     - `report_pack.sections` has neither figure.
     - `report_pack_price`, one row per pack, holds the summary as the customer sees it:
       the price, no cost. Its RESTRICTIVE `price_visibility` policy admits commercial
       roles, and a customer scope once the pack is published. Only commercial roles may
       insert, and nobody may update.
     - `report_pack_commercial` holds the summary with both figures.
     - The API merges the three in that order, so staff in commercial roles still see
       cloud cost and the approver sees the customer's figure.
     - The narrator drafts from `report_pack.sections`, so no narrative carries either
       figure.
   - The staff figure is labelled "Cloud cost (unblended)". The price is on an amortized
     basis, so in a month with a reservation purchase the two differ, and the label says
     why. A month with no billing rows has no price; it is never zero.

## Consequences

- **Commercial must set each customer's uplift on its SOW before that customer's portal
  shows a price.** Until then the customer sees "Not yet priced". There is no capture
  screen for SOW fields yet (ADR 0019: SOW capture is a dependency), so it is set by
  migration or seed for now.
- **Customer report packs (step 17, ADR 0020) show this price where the cost was.** See
  decision 8.
- **The gateway tool `get_cost_trend` reads `amortized_cost`.** A customer scope calling it
  now gets permission denied (fail closed). If customers reach the assistant, the tool
  should read `cloudops.customer_price` for them instead.
