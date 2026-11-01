# Cost and FinOps
audience: staff

## What the FinOps screen shows

FinOps is a Getronics staff screen. It shows each customer's cloud cost for one invoice month, split into consumption and other spend, with a reconciliation verdict beside it.

Each row in the "Cost by customer" table shows the Customer, its Reconciliation status (Verified or Unverified, with the reason), Consumption, Other spend, the share of cost that is unallocated by application and by cost centre, and the number of cost anomalies in the last 30 days.

Choose a customer to open its own cost page for the month. Customers never see this screen. They see their consumption on the Customers screen, at the price they are charged, for closed months only.

## Choosing the month and reporting currency

The controls at the top choose the invoice month and an optional reporting currency. The choice is kept in the page address, so a link you share or a reload shows the same view.

Amounts always stay in the currency they were billed in. With the reporting currency set to "None (as billed)", you see totals per billing currency only.

If you choose a reporting currency, each amount is also converted using the daily rate in force on its own usage date, and the converted total appears under the original amounts, marked with "≈". If any day lacks a rate for that currency, the converted total is not shown and the cell says "No rate for every day" instead of showing an incomplete figure.

## Consumption and other spend

Charges are grouped into classes. Consumption is the metered use of cloud services, and it is the only class that feeds margin. Other spend covers licences, marketplace purchases, support, reservations, tax and credits; these are shown but do not feed margin.

On a customer's cost page, the "Charges by class" table lists each class with whether it feeds margin, its amortized cost, its unblended cost, and, if a reporting currency is chosen, the converted figure.

Amortized cost spreads up-front purchases such as reservations across the period they cover. Unblended cost is what was charged on the day.

## Reconciliation: Verified and Unverified

Every month's cost is checked, account by account, against the provider's own total or against the invoice. The result is shown as a badge:

- Verified: every account matches the provider's own total.
- Unverified: at least one account does not, or has not been checked.

An unverified figure is shown in italics with an "Unverified" mark, so it never reads as a normal figure. The reason is given in words, for example that an account drifts by a percentage from the provider's total, that no provider total has arrived yet, that the ledger and the reference are in different currencies, or that an account is billed in more than one currency.

On the customer page, "Reconciliation checks" lists each account, what it was checked against, the status, our ledger figure, the provider's figure, the drift and the date checked. Reconciliation runs daily once the provider publishes a total.

## Allocation and unallocated cost

Cost is allocated to applications and cost centres from each resource's tags. On a customer's cost page, two tables show the allocation by application and by cost centre, each with currency and amortized cost.

Cost with no tag, or cost that cannot be matched to a resource, is shown as its own "Unallocated" row. It is never folded into another line or hidden. On the FinOps list, the unallocated columns show what share of the month's cost is unallocated.

A high unallocated share usually means missing tags. Raise it with the customer's cloud owner.

## Cost anomalies

The "Cost anomalies" table on a customer's cost page lists days or months where an account's cost moved outside its expected range. Each row shows when it was observed, the account, the window (Day or Month), whether it was a Spike or a Drop, the observed cost, the expected baseline, and the change against baseline as a percentage. "No baseline" means there was not yet enough history to set one.

"No anomalies" means daily and monthly cost stayed within the expected range for every account. On the FinOps list, the anomaly count covers the last 30 days and is shown as a Watch badge when it is above zero.

## FX rate corrections

The FinOps screen also lists FX rate corrections. Exchange rates come from the ECB reference rates feed. When Finance corrects a rate, the correction is recorded as a new entry with its reason; the original is never overwritten.

Each entry shows the currency pair, rate type, effective date, rate, source, reason and when it was entered. Closed months affected by a correction are restated at the next month-end run as new versions, and earlier versions are kept.

Only roles with permission to manage FX can record a correction. Everyone else sees the list and, if a rate looks wrong, should ask Finance.

## If there is no cost

"No cost for this month" means no charges were recorded for any customer in scope for that month. Choose another month, or check the billing connectors on the Connectors screen.

On a customer page, "No charges this month" and "No cost to allocate" mean nothing was billed to that customer that month. "No checks for this month" means reconciliation has not run yet, typically because the provider has not published its total; until then the cost is Unverified.
