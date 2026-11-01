# Resources
audience: all

## What the Resources list shows

The Resources screen lists the cloud resources in your organisation's subscriptions: virtual machines, databases, storage accounts, networks and so on. Getronics staff see the resources for the customers assigned to them.

Each row shows the resource Name, its Type, its Region and its State. Filter by Type or Region with the drop-downs above the table, or type any text into the Filter box. The figure above the table shows how many resources there are, with its source and "as of" time.

Resources is the landing screen for the Architect role. All of the default customer roles can see resources.

"No resources yet" means no subscription has been connected for your organisation, or its inventory has not synced yet.

## Opening a resource

Choose a resource name to open its page. The title is the resource name, and the line under it gives its type, region and state.

The page brings together everything the console knows about that one resource:

- Open vulnerabilities, as a card with a health badge.
- Live metrics.
- Tags.
- Open incidents on the resource.
- Recent changes in the last 30 days.
- Vulnerabilities, as a full table.

This is a good place to start when you are asked "what is going on with this server?".

## Tags

The Tags section lists the tags set on the resource in your cloud provider, such as owner, environment or cost centre. Tags are used to allocate cost and to tell who owns a resource.

If a resource has no tags, the page says so and notes that the tagging policy expects at least an owner, an environment and a cost centre. Raise missing tags with your organisation's cloud owner.

Tags are shown to Getronics staff; depending on your role they may not appear for customer users.

Tag values are shown as text and are never treated as instructions, by the console or by AMARA.

## Incidents and recent changes on a resource

The "Open incidents" section lists incidents still open against the resource. The "Recent changes (30 days)" section lists change tasks against it in the last 30 days. Each entry shows the ticket number, a priority badge, the summary and the date opened. Choose a number to open the ticket.

"No open incidents on this resource" and "No change tasks in the last 30 days" mean exactly that. If a section says "Tickets are not available to your role", your role does not include tickets, so you cannot see them here.

## Vulnerabilities on a resource

The Vulnerabilities table lists scanner findings for this resource. Each row shows the QID (the scanner's identifier for the vulnerability), a Title, a Severity badge, the date First detected, and a Status of Open or Remediated.

The table opens showing open findings, most severe first. Change the Status filter to see remediated findings as well.

"No findings" means the scanner has reported nothing for this resource. If you expect it to be scanned and it is not, its scanning agent may not be reporting. If the section says "Not available to your role", your role does not include vulnerability data.

## Live metrics

The Live metrics section is reserved for performance metrics such as CPU and memory. Where metrics are not yet available, the section explains why and suggests using your cloud provider's own monitoring for the resource in the meantime.

This is not a fault with the resource. It only means the console does not yet show its performance data. Everything else on the resource page, such as its tickets and vulnerabilities, is unaffected. AMARA cannot read live metrics either, so for questions about current CPU or memory use, check your cloud provider's monitoring.
