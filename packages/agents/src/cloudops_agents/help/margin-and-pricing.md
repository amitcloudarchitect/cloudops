# Margin and pricing
audience: staff

## Who can see margin and pricing

Margin and Pricing are Getronics staff screens. They contain commercial figures, which are visible only to authorised Getronics staff. Customers never see them, and AMARA never discusses them with a customer user.

If you open one of these screens and see "Not available to your role", your role does not include commercial figures. If you believe you need access for your job, ask a platform administrator. Do not copy figures from these screens into material that customers will see.

## What the Margin screen shows

Margin shows each customer's margin for one month, in the contract currency. The "Margin by customer" table has, for each customer:

- Band: Healthy, Watch, At risk or Critical, on the console's usual scale.
- Margin %.
- Gap to target: the difference from the target band in the statement of work (SOW).
- Actual and modelled margin.
- Effort.
- Status: Provisional or Closed, with "restated" where a figure has been recomputed.
- Trend: margin over the last six months.

Choose the month and an optional reporting currency at the top. Choose a customer to see its detail.

## Provisional, closed and restated margin

During the month, margin is Provisional: figures are shown for the month to date, but there is no band and no gap to target. Those read "Banded at month close" and "At month close" until the month closes.

Once the month closes, the figure becomes Closed and is banded.

A restatement, for example after an FX correction, produces a new version of the figure. Earlier versions are kept as they were. A customer's margin page lists every version with its status, when it was computed, which version it restates, and its margin.

If the band was set on modelled cost because effort data is incomplete, the band carries the note "On modelled cost: effort incomplete". If the cost behind it did not reconcile, it carries "Cost not reconciled".

## When margin is Not captured

Margin that cannot be computed is shown as "Not captured" with the reason, never as zero or a dash. The reasons are:

- No SOW: there is no revenue to set cost against. Margin is captured once the SOW is registered.
- No people cost for the month.
- No FX rate for the contract currency.
- Not computed for this month yet.
- No cost data for the month.

"No target in the SOW" in the gap column means the SOW has no target band to compare against.

## A customer's margin detail

A customer's margin page shows Margin % with its band, Gap to target, Revenue, Actual cost, Modelled cost, Actual margin, Modelled margin and Effort, all in the contract currency.

With a reporting currency chosen, it adds the margin converted at the month's average rate and the FX impact as its own line. Margin is always computed in the contract currency first, then converted.

Warnings may appear: "Effort exceeds the priced slab", or a count of critical configuration items with no catalogue price.

The "Variance by workgroup and activity type" table shows hours, work items, share of hours, and whether effort is beyond the priced slab.

## What the Pricing screen does

Pricing quotes a managed service from an inventory, using the rate card in force. The header names the rate card version, the dates it is in force, and its currency. Quotes are in EUR.

You enter counts of critical configuration items by resource type, the month's cloud consumption in EUR, and the coverage, support model and delivery location. The quote shows total per month, price per CI, service cost, base pack, cloud services cost, platform cost, critical CIs, how far it is to the next slab, the implied team by role, and the transition period in weeks. "Repricing due" warns that a customer is close to the next slab.

Quotes are modelled figures and are not stored. If no rate card is in force for the date, the screen says so.
