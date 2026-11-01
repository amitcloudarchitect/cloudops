# Work queue and tickets
audience: all

## What the Work queue shows

The Work queue lists incidents, change tasks, service requests and project tasks from the service desk (ServiceNow) for your organisation. Getronics staff see the tickets for every customer assigned to them.

Each row shows the ticket Number, its Kind, its Priority, a Summary, the Configuration item it concerns, the date it was Opened, and its SLA status. When you can see more than one customer, a Customer column is added.

Tickets sync from the service desk every few minutes, so a ticket raised a moment ago may take a short while to appear. The figure above the table shows how many tickets match, with its source and "as of" time.

The Work queue is the landing screen for the Customer engineer role.

## Open, closed and all tickets

The Show selector at the top right switches between Open (the default), Closed and All tickets.

The list shows the newest tickets first when there are many. If a note says "The newest N of M are shown", there are more tickets than fit in one list. Narrow it with Show, or search by ticket number in the Filter box.

"No open work for your customers" means there are no open tickets. Choose Show: All to see closed work as well.

## Kinds of ticket

The Kind column uses these labels:

- Incident: something is broken or degraded and needs restoring.
- Change task: a piece of work carried out as part of a planned change.
- Request: a service request, such as asking for access or a new resource.
- Project task: work that belongs to a project rather than day-to-day operations.

Use the Kind filter above the table to show only one kind.

## Priority and SLA columns

The Priority column shows the service desk priority as a coloured badge. Priority 1 is shown as Critical, priority 2 as At risk, priority 3 as Watch, and priorities 4 and 5 as Healthy. A ticket with no priority shows "None".

The SLA column says whether the ticket has met its service level:

- Breached, shown as a red badge, means the ticket has already missed its target time.
- Within means it has not breached.
- A dash means no service level information is available for that ticket.

Use the SLA filter to show only breached tickets. Breached does not necessarily mean nobody is working on it; it means the target time has passed.

## Preset and saved views

The View menu offers preset views that suit your role. A customer engineer, for example, has "Incidents" (newest first) and "SLA breached" (oldest first). A customer viewer has "Incidents". Other roles start from the full list.

You can build your own view with the filters and sort, then choose "Save view". Saved views are kept in your browser. "Export CSV" downloads the rows the table is currently showing.

When you arrive from a card on the Customers or Performance screen, the queue opens already filtered, for example to breached tickets only.

## Opening a ticket

Choose a ticket number to open its detail. The page shows the ticket's summary and a Priority badge, then its details:

- Customer, State and Workgroup.
- Opened and Closed times (Closed reads "Open" while it is still open).
- SLA: Breached, Within, or a dash.
- Change request, if the ticket belongs to one.
- Configuration item. If your role can see resources and the item is a known resource, it is a link to that resource.
- Resolve time, in hours and minutes, once resolved.
- Closure code and Close notes, once closed.

Use "Work queue" at the top of the page to go back to the list.

## Changing a ticket

The console does not change tickets. To raise, update, reassign or close a ticket, use the service desk as you do today. Your change appears in the console after the next sync, usually within a few minutes.

AMARA cannot change tickets either. It can help you find a ticket, summarise what the console shows about it, or explain a field.

## Agent triage on tickets (Getronics staff)

Getronics staff see extra information on the Work queue and ticket pages: a Workgroup column and a Triage column, and on the ticket page an "Agent triage" panel.

New incidents are triaged automatically. The panel shows the triage route, either "Needs a person" or "Ready for planning", the classification (such as intent, affected configuration item, action class, risk and confidence), and any enrichment added. If triage stopped early, "Why it stopped here" lists the reasons.

If the ticket text contained instructions aimed at the automation, a warning says so. Those instructions were treated as data and not followed; review the classification carefully.

The panel never proposes or takes an action in a customer environment. "Proposed plan" reads "None". Staff whose role allows it can choose "Accept triage", or "Override" to change fields with a required reason. Every review is listed with its time. If the ticket has not been triaged, the panel reads "Not triaged yet".
