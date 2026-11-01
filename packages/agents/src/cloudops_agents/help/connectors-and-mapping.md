# Connectors and customer mapping
audience: staff

## What connectors are

Connectors are the scheduled jobs that bring data into the platform from source systems: the service desk, the vulnerability scanner, cloud inventory and billing, document sites and others. Each connector runs on a schedule for one source and one kind of record, such as incidents or resources.

When a connector is late or failing, the screens that depend on it show older data. The "as of" time on a figure is the quickest way to tell. The Connectors screen, for Getronics staff, shows the health of every connector in one place.

## The Connectors screen

The Connectors screen shows ingest health for every scheduled source. Summary cards at the top count Outages, Late or failing connectors, those that Need attention, and those that have Never run.

The table lists each connector with:

- Health, as a badge with its status.
- Last success, as how long ago.
- Expected every, the schedule interval.
- Failed runs and rejected records in the last 24 hours.
- Quarantine: records held because their customer name or resource could not be matched. A link takes you to Customer mapping.
- Dead letters: records that could not be processed and wait for a person.
- Open drift: schema changes not yet acknowledged.

Filter by Source or Status.

## How connector health is decided

The screen's "How health is decided" section explains the statuses:

- Late: more than the allowed number of intervals since the last successful scheduled run.
- Outage: much longer than that, or runs that have never succeeded.
- Failing: a high share of the last 24 hours' runs failed.
- Needs attention: a failed run, rejected records or open dead letters in the last 24 hours, or schema drift nobody has acknowledged.
- Never run: scheduled, but no run is recorded yet.

These map onto the console's usual health bands. Replays do not count as scheduled runs. The band is decided by the platform; the screen shows it with its reasons.

## Schema drift

Schema drift means a source has started sending a field the platform did not expect, or has stopped sending one it did. The drift table lists the Connector, the Field, the Change ("New field" or "Field missing"), when it was last seen and how many times.

Once the mapping has been updated, or once you decide no change is needed, choose Acknowledge. Only roles allowed to do so can acknowledge drift. "No open drift" means every source sends the fields its mapping expects.

## What customer mapping is for

Source systems name customers in their own way. The service desk may call a customer one thing and the billing export another. Customer mapping connects each name a source sends to the right customer in the platform.

When a record arrives with a name that matches no customer, it is not guessed and not dropped. It is held in quarantine, and the platform counts how many times the name has been seen. Nothing held in quarantine appears on any customer's screens until it is mapped.

The Customer mapping screen is for Getronics staff whose role allows it.

## Mapping a quarantined name

The Customer mapping screen shows the number of names in quarantine and the sightings held. The "Quarantined names" table lists each name as sent, its source, how many sightings, when it was last seen and any suggested customer.

To map a name, choose Map, then pick an existing customer, or onboard a new customer by giving its name and a short code of 2 to 10 capitals or digits. The mapping is then proposed.

A proposal must be approved before it takes effect. Depending on the environment, a second person must approve it. The person approving can approve or reject; a rejection needs a reason. Every decision is listed under the decisions history, newest first.

## Re-processing held records

An approved mapping applies to records that arrive from then on. Records already held under that name stay in quarantine until someone asks for them to be re-processed.

To do this, choose re-process on the approved mapping. The request shows its state: Waiting for the worker, Running, Done, Refused or Failed. Requests are usually picked up within a minute and the page refreshes until the request ends.

A re-process is refused if nothing was held under the name, if the name no longer maps to that customer, or if the mapping is not approved.

The "Customers and their names" table lists each customer, its short code, status and the names it is known by per source.
