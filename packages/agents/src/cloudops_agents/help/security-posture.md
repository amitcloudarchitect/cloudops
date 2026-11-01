# Security posture
audience: all

## What the Security posture screen shows

Security posture shows the open vulnerability findings reported by the vulnerability scanner (Qualys) for your organisation's resources. It answers two questions: how severe are the open findings, and how long have they been open?

At the top are summary cards: the total number of open findings, then one card per severity level. Below those are three cards by age: open up to 30 days, open 31 to 90 days, and open over 90 days. Below the cards is a table of every open finding, most severe and oldest first.

It is the landing screen for the Security officer role. Roles without vulnerability access do not see this screen.

## Severity levels

Findings are graded from 1 to 5 by the scanner. The console shows the number and a name:

- 5, Urgent
- 4, Critical
- 3, Serious
- 2, Medium
- 1, Minimal

On the severity badges, severity 5 is coloured Critical, severity 4 At risk, severity 3 Watch, and severities 1 and 2 Healthy.

The cards for severities 4 and 5 also carry a health badge based on how many findings are open at that severity. One or more is Watch, five or more is At risk, and twenty or more is Critical.

## The findings table

Each row in the table shows:

- Severity, as a badge with its number and name.
- Finding, the title of the vulnerability.
- QID, the scanner's identifier for that vulnerability.
- Asset, the resource it was found on. If the asset is a known resource, its name is a link to the resource page.
- First detected, the date the scanner first reported it.
- Age (days), how long it has been open.

Use the Severity filter to show one level, or the Filter box to search for a title, QID or asset. "Export CSV" downloads the rows you see.

## How findings appear and disappear

Findings appear here when the scanner reports them and leave when they are remediated, that is, when a later scan no longer finds the problem. The console does not mark findings as fixed itself.

"No open findings" means the scanner currently reports nothing open for your organisation. If you expected findings and see none, check the "as of" time on the cards to see when data last arrived.

Remediated findings are not listed on this screen. To see the full history for one resource, including remediated findings, open the resource page from Resources.

## Critical vulnerabilities on other screens

The Customers screen shows a "Critical vulnerabilities" card, and each resource page shows an "Open vulnerabilities" card. Both use the same health bands as the severity 4 and 5 cards here: one or more is Watch, five or more At risk, twenty or more Critical.

If one of these cards says "Not available to your role", your role does not include vulnerability data. That is not the same as having no vulnerabilities.
