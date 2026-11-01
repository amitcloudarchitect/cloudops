# Customer overview
audience: all

## What the Customers screen shows

The Customers screen is a one-page summary of your services. If you are a customer user, its title is your organisation's name and its subtitle reads "Your services at a glance". If you are Getronics staff looking after several customers, it is titled "Customers" and shows one block per customer.

Each block has a row of cards:

- Resources: how many cloud resources are in your subscriptions.
- Open incidents: incidents not yet resolved.
- SLA breached, open: open tickets that have already missed their service level.
- Critical vulnerabilities: open findings at severity 4 or 5.
- A cloud cost or consumption card.

Every card shows where its figure came from and when, under the number. It is the landing screen for the CXO and Customer viewer roles.

## How the cards are coloured

Three of the cards carry a health badge: Open incidents, SLA breached open, and Critical vulnerabilities. The badge is Healthy, Watch, At risk or Critical depending on how high the count is, and always names the band in words as well as colour.

When Getronics staff see several customers, the customers are ordered most at risk first. A customer's position is set by its worst card, so a customer with one critical card appears above a customer with several cards on Watch. Customers with the same worst band are listed alphabetically.

For what each band means and the counts behind them, see the page on metrics and health bands.

## Following a card to the evidence

Many cards are links. Under the figure you will see "View the evidence". Choosing it takes you to the list behind the number:

- Resources opens the Resources list.
- Open incidents opens the Work queue filtered to incidents.
- SLA breached, open opens the Work queue filtered to breached tickets.

These links appear only if your role can see the screen they lead to. A security officer, for example, sees the vulnerabilities figure but may not have a link to tickets.

If a card says "Not available to your role", your role does not include that kind of data. It is not a zero.

## Cloud consumption for customers

If you are a customer user, the cost card is titled "Cloud consumption" followed by a month and currency, for example "Cloud consumption, October 2026 (EUR)". It shows your consumption for the last complete calendar month at the price Getronics charges you. It is not Getronics' own cost.

The figure covers a closed month only. The current month is not shown until it closes, so the card will not change day by day.

If you are billed in more than one currency, you see one card per currency.

If the card says "No consumption last month", nothing was consumed. If it says "Not yet priced: your Getronics service manager can confirm the rate", your consumption was recorded but the price that applies has not been captured yet. Your service manager can confirm the rate. The figure is never shown as zero while it is missing.

## Cloud cost for Getronics staff

Getronics staff may see a card titled "Cloud cost, 30 days" with a currency, showing cloud cost over the last 30 days in each billing currency. "No cost in the last 30 days" means nothing was billed in that window.

This is an operational view. Commercial analysis of cost, such as reconciliation and margin, is on the staff FinOps and Margin screens and is visible only to authorised Getronics staff.

## If the screen is empty

"No customers assigned" means your account has no customers in its scope yet. For Getronics staff, a platform administrator assigns customers to your account. Customer users should not normally see this message; if you do, contact your organisation's portal administrator.

A card with no figure and a short explanation means the data is not available yet, for example because the relevant source has not synced for your organisation. Check the "as of" time on the other cards to see how recent the data is.
