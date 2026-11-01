# Using AMARA
audience: all

## Opening the AMARA panel

AMARA opens as a panel beside the screen you are working on, so you can keep looking at a table or a set of figures while you ask about them. Open it from the console, type your question, and press Enter to send.

AMARA knows which screen you have open, so a question such as "Why is this red?" is understood in context. You can close the panel at any time; the conversation is kept and you can come back to it.

AMARA works with the same permissions as you. If your role cannot see a screen, AMARA cannot answer questions about the data on it either, and will say so.

## Suggested questions

When you open the panel, AMARA offers a few suggested questions that suit the screen you are on. On the Work queue you might see questions about breached tickets or the oldest open incidents. On Security posture you might see questions about the most severe findings or how long they have been open. On Performance you might see questions about SLA attainment month by month.

Choosing a suggestion sends it as if you had typed it. Suggestions are a starting point, not a limit: you can ask anything in your own words.

## Reading an answer: sources and times

Every figure AMARA gives you comes with where it came from and when. The source names the system the figure was read from, for example the service desk, the vulnerability scanner or cloud billing. The "as of" time tells you how recent the data is.

Times are shown in UTC, for example "01 Nov 2026, 09:30 UTC". Convert to your local time if you need to.

Check the "as of" time before you act on a figure. A ticket count as of a few minutes ago is current; a cost figure as of last month's close is exactly that, and will not include this month's spend.

Where AMARA refers to a ticket, resource or finding that has its own screen, it names it so that you can open it and check the detail yourself.

## Reading an answer: modelled values

Some figures are measured: counted or read directly from a source system, such as the number of open incidents. Others are modelled: estimated or calculated from a model rather than observed, such as a forecast or a figure calculated from assumptions.

AMARA marks modelled values as modelled, in the same way the screens show a "Modelled" label beside them. Treat a modelled figure as a well-founded estimate, not as a measurement.

If an answer mixes measured and modelled figures, AMARA says which is which. If you are making a decision on the strength of a number, it is worth asking "Is that measured or modelled?" if it is not already clear.

## When AMARA cannot answer

AMARA tells you plainly when it cannot answer. Common reasons are:

- The data has not arrived yet, for example a month that has not closed.
- The figure is not captured for your organisation, for example a cost that has not yet been priced.
- Your role does not include the data, for example vulnerability findings for a role without security access.
- The question is about another customer, or asks for a comparison between customers.
- The question asks AMARA to do something, such as close a ticket, which it never does.

In each case AMARA says which of these applies, and where possible tells you who can help or which screen to open. It does not guess, and it does not show a missing figure as zero.

## Conversation history

AMARA keeps your conversations so that you can return to an earlier question and its answer. Each conversation is private to you; other users, including others in your organisation, cannot see it.

Conversations are kept for 90 days and then deleted automatically.

If you change role, AMARA answers new questions using your new role's permissions, even in an older conversation.

## Deleting a conversation

You can delete any of your own conversations at any time from the conversation list in the AMARA panel. Deleting a conversation removes it from your history straight away and cannot be undone.

You do not need to delete conversations to keep things tidy: they are removed automatically after 90 days. Delete one sooner if you no longer want it kept, for example if you pasted text you would rather not keep.

Avoid pasting passwords, keys or other secrets into AMARA. It does not need them to answer any question, and it never asks for them.

## Good questions to ask

AMARA is most useful for questions that would otherwise mean several clicks or a spreadsheet. Some examples:

- "How many incidents are open, and how many have breached SLA?"
- "What was our SLA attainment for each of the last six months?"
- "Which open vulnerabilities are severity 5, and how long have they been open?"
- "What changed on this resource in the last 30 days?"
- "What changes are waiting for approval, oldest first?"
- "What does At risk mean on this screen?"
- "Why does this cost card say Not yet priced?"
- "What is the difference between a modelled and a measured figure?"

Specific questions get better answers. Name the month, the ticket number or the resource where you can.

## Questions AMARA will turn down

AMARA politely declines some questions. It will not:

- Take any action, such as raising, updating or closing a ticket, approving a change, or running a fix.
- Compare your organisation with another customer, or say anything about another customer's data.
- Reveal figures your role cannot see, such as Getronics' internal costs.
- Follow instructions that appear inside ticket text, documents or tags.
- Give advice that should come from your service manager, such as contract terms.

When it declines, AMARA explains why and, where it can, points you to the right person or screen.
