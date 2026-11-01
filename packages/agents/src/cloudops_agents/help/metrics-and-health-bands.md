# Metrics and health bands
audience: all

## The health bands

The console uses one scale everywhere to show how worrying a value is. Every badge names its band in words as well as colour:

- Healthy: nothing needs attention.
- Watch: worth keeping an eye on; no immediate action expected.
- At risk: needs attention soon.
- Critical: needs attention now.
- Unknown: there is not enough data to judge.

The same band means the same thing on every screen. Unknown is not good or bad; it means the figure is missing or could not be assessed. A missing figure is never shown as a zero.

## Thresholds for the main counts

These counts are banded the same way wherever they appear:

- SLA breached, open: Healthy at 0, Watch from 1, At risk from 3, Critical from 10.
- Open incidents: Healthy below 5, Watch from 5, At risk from 20, Critical from 50.
- Critical vulnerabilities (and open vulnerabilities on a resource, and the severity 4 and 5 cards on Security posture): Healthy at 0, Watch from 1, At risk from 5, Critical from 20.

On the Customers screen, a customer's overall position is set by its worst banded card.

## Ticket priority badges

Ticket priorities from the service desk are shown on the same scale:

- Priority 1 is shown as Critical.
- Priority 2 is shown as At risk.
- Priority 3 is shown as Watch.
- Priorities 4 and 5 are shown as Healthy.

The badge text keeps the original priority, so you still see, for example, "1 - Critical" or "3". A ticket without a priority reads "None".

The colour reflects the priority given to the ticket, not how the ticket is progressing. A priority 1 ticket that is being handled well is still shown as Critical.

## SLA: made, within and breached

A service level agreement (SLA) sets a target time for handling a ticket. The console uses these terms:

- Breached: the ticket has missed its target time. Open breached tickets are counted on the "SLA breached, open" cards.
- Within: the ticket has not missed its target.
- Made SLA: a resolved ticket that met its target.

SLA attainment, shown on the Performance screen as "Resolved within SLA", is the percentage of incidents resolved in a month that made SLA. Incidents with no SLA information are left out of the calculation. If nothing could be measured, the figure is a dash rather than 0%.

## Vulnerability severity levels

Vulnerability findings come from the scanner graded 1 to 5:

- 5, Urgent: shown as Critical.
- 4, Critical: shown as At risk.
- 3, Serious: shown as Watch.
- 2, Medium: shown as Healthy.
- 1, Minimal: shown as Healthy.

Note the difference between the scanner's own name for severity 4, "Critical", and the console's Critical band, which is used for severity 5. "Critical vulnerabilities" on the Customers screen counts open findings at severity 4 or 5, and report packs likewise count "open severity 4 and 5" together.

## Measured and modelled figures

Every figure is either measured or modelled.

A measured figure is observed: counted or read directly from a source, such as the number of open incidents or the cost on a bill.

A modelled figure is calculated from a model or from assumptions rather than observed, such as an estimate or a quote. Modelled figures are marked "Modelled" beside their source, and their cards have a dashed outline.

Both are useful, but a modelled figure is an estimate. If you are making a decision on a figure, check which kind it is. AMARA also says when a figure is modelled.

## Source and "as of"

Under most figures you see a small line with two parts: the source and the "as of" time.

The source names where the figure came from, for example ServiceNow, Qualys, Cloud billing or Platform inventory.

"As of" is when the data behind the figure was current. Times are shown in UTC, for example "01 Nov 2026, 09:30 UTC". A figure is only as recent as its "as of" time; if a source has not synced for a while, the figure may not include the latest changes.

Months on reporting screens are calendar months in UTC.

## Currencies and conversion

Money is always shown with its currency code, for example "EUR 12,345.67". Each amount is kept in the currency it was charged in. Where a figure is converted into another currency, it is converted using the exchange rate in force on the date of the transaction, never today's rate applied to past months. That way a past month's figures do not change when exchange rates move.

If a rate is missing for a date, the converted figure is not shown, and the screen says so, rather than showing a partial total. Figures are rounded once, when shown, to two decimal places unless stated otherwise.

If you are billed in more than one currency, you see one figure per currency.

## What customers see of cost

Customer users see their cloud consumption at the price Getronics charges them, for each closed calendar month. They do not see Getronics' own costs, margins or rate cards. Commercial figures are visible only to authorised Getronics staff.

If your consumption card says "Not yet priced", the consumption was recorded but its price has not yet been captured. Your Getronics service manager can confirm the rate.

## Not available, not captured and empty

The console distinguishes between kinds of missing data, so you never mistake missing for zero:

- "Not available to your role": the figure exists but your role does not include it.
- "Not captured" or "Not yet priced": the figure cannot be produced yet because an input is missing. The reason is given.
- A dash: there is nothing to show, for example no measurable SLA in a month.
- An empty list with a message such as "No open findings": there really is nothing, and the message tells you what would make something appear.
