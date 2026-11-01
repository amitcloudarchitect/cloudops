# Assurance
audience: staff

## What the Assurance screen shows

Assurance is a Getronics staff screen for assessing a customer against a control framework. Controls are answered from the platform's own data where possible, drafted where they need a written answer, and scored only once a person approves each result.

The screen lists the control libraries that are loaded, then the assessments in your scope. Each assessment row shows its title, the Customer, the Framework and library version, the Score, the number of controls Awaiting approval, and the date Opened.

If the screen says "No control library is loaded", there is nothing to assess yet. A control library has to be supplied and loaded by a platform administrator first.

## Opening an assessment

Staff whose role allows it can open a new assessment using the form at the top: choose the customer and the framework, and give it a title. Opening an assessment creates one response for every active control in that framework.

Choose an assessment in the list to open its detail page. The header shows the title, the customer, the framework and version, and when it was opened.

## Working through the controls

On an assessment page, three buttons act on the whole assessment:

- Collect evidence gathers evidence from the platform's data for every automated control.
- Request drafts asks for AI drafts for the controls that need a written answer. The drafts arrive shortly.
- Export signed evidence pack downloads the evidence as a signed pack.

Each control shows its code and title, its domain, how it is assessed, its requirement, and its current result or "Awaiting approval". Where a rule proposes a result from the evidence, it is shown as "Proposed by the evidence rule" with its basis. You can expand the evidence to see what was retrieved and when.

For narrative controls, the AI draft is shown with its citations and a note on how sufficient the material was. A draft is never a result. Write or edit the narrative yourself ("Start from the draft" copies it in), save it, then approve a result.

## Results and the score

Each control ends with one of these results, approved by a person:

- Compliant, coloured Healthy.
- Partially compliant, coloured Watch.
- Non-compliant, coloured Critical.
- Not applicable.
- Manual.

The score counts approved results only. Compliant scores 100, partially compliant 50 and non-compliant 0, and the score is shown as a percentage. Manual and not applicable controls are excluded. Until anything is approved, the score reads "Not scored: nothing approved yet".

The cards show the Score, Scored controls, Awaiting approval, and Manual or not applicable. If someone changes a narrative while you are reading it, the console asks you to reload before approving.
