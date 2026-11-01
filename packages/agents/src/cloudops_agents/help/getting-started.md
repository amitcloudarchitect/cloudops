# Getting started
audience: all

## What the console and portal are

The Getronics CloudOps console is the place where you see how the cloud services Getronics manages are running. Getronics staff use it as an operations console across the customers they look after. Customer users reach the same application as a customer portal, showing only their own organisation's services.

Everything you see is gathered from the systems that run your service: tickets from the service desk, the cloud resources in your subscriptions, vulnerability scan findings, and cloud cost. The console brings these together so you can answer everyday questions without switching tools: what is open, what is late, what is at risk, and what it costs.

The console is mostly for reading. A few screens let authorised people record a decision, such as a customer administrator adding a user. Day-to-day actions such as raising a ticket or approving a change still happen in the service desk as they do today.

You only ever see data for your own organisation, or, for Getronics staff, for the customers assigned to you.

## Signing in

You sign in with your normal work account. Getronics staff use their Getronics account. Customer users sign in with their own organisation's account; there is no separate password for the portal.

When you open the console you are sent to Microsoft's sign-in page and then returned. If sign-in does not complete, the console says so and offers "Sign in again". If it keeps failing, note the code shown in the message and quote it when you ask for help.

If you sign in successfully but see "Your account has no access yet", your account is recognised but has not been given a role. Getronics staff should ask a platform administrator. Customer users should ask their organisation's portal administrator. The message shows your object id: give that to the administrator, who uses it to add you, then sign in again.

To leave, choose "Sign out" in the header.

## Where you land

After you sign in, the console opens on the screen that suits your role. A customer security officer, for example, opens on Security posture, an approver on Approvals, and a customer engineer on the Work queue. If your role has no particular landing screen, you open on the first module your role can use.

If you see "No modules for your role", your account exists but your role does not include any screen. Ask a platform administrator or your organisation's portal administrator which role you need.

You can always return to your landing screen by choosing "CloudOps Operations" at the top left of the header.

## The navigation

The navigation list sits down the left side on a wide screen, and across the top on a narrow one. It shows only the modules your role can use, so two people in the same organisation may see different lists. If someone else mentions a screen you cannot see, your role does not include it.

The modules you might see include Work queue, Performance, Security posture, Approvals, Resources, Customers and Reports. A customer administrator also sees Users. Getronics staff may see further modules for their job.

If you follow a link or type an address for a screen your role does not include, the console shows "Not available to your role" rather than the data. Choose another module from the navigation, or ask an administrator if you believe your role should include it.

## Tables, filters and saved views

Most lists in the console use the same table. You can sort by clicking a column header. The Filter box matches text in any column. Drop-down filters above the table, such as Kind, Severity or Region, narrow the list to one value; "Any" clears them.

The View menu offers preset views chosen for your role, for example "Incidents, oldest first" or "SLA breached". When you have a combination of filters and sort you like, choose "Save view" and give it a name. Saved views are kept in your browser, for your role, so they will not follow you to another computer. "Delete view" removes one you saved.

"Export CSV" downloads exactly the rows the table is showing, with the filters applied, as a spreadsheet file.

If a table says "Nothing matches this view", clear the filter or choose another view.

## The theme setting

The header has a Theme setting with three choices: System, Light and Dark. System follows your computer's own light or dark preference, and is the default. Light and Dark fix the look regardless of your computer.

Your choice is remembered in this browser. If your browser blocks storage, the console falls back to following your system, and a choice you make lasts only until you reload the page.

Health colours keep the same meaning in both themes, and every coloured badge also names its status in words, so you never have to rely on colour alone.

## What AMARA is

AMARA is the AI assistant built into the console. You can ask it questions in plain English about how the platform works and about the figures you are allowed to see, such as "How many incidents breached SLA last month?" or "What does Watch mean?".

AMARA answers using the same data and the same permissions as the screens. It cannot see anything your role cannot see, and it tells you where each figure came from and how recent it is.

AMARA is there to save you time finding and explaining information. It is not a replacement for your Getronics service manager or for the service desk. For anything that needs a decision or an action, use the normal route.

## What AMARA will not do

AMARA is read-only. It answers questions; it never changes anything.

It does not raise, update or close tickets. It does not approve or reject changes. It does not run fixes, restart resources or touch your cloud environment in any way. It does not add users or change roles. If you ask it to do one of these things it will explain how to do it through the normal route instead.

AMARA can say "I don't know". If the data to answer a question is missing, out of date or outside what your role can see, it says so rather than guessing. Treat a clear "I don't know" as a correct answer.

AMARA answers about one customer account at a time. It will not compare one customer with another, and it will not discuss another organisation's data. Getronics staff who look after several customers should ask about each one separately.

AMARA also treats text inside tickets, documents and tags as information, not as instructions. If a ticket contains a request aimed at the assistant, AMARA will not follow it.

## Getting help

If a figure looks wrong, start by checking its source and "as of" time, shown beside it. Many apparent gaps are simply data that has not arrived yet; tickets, for example, sync every few minutes.

If something still looks wrong, customer users should contact their Getronics service manager or raise it through the service desk as usual. Getronics staff should raise it through their normal internal support route.

For questions about your own access, customer users should ask their organisation's portal administrator, and Getronics staff a platform administrator.
