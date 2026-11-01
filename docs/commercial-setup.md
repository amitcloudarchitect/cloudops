# Commercial setup: entering a SOW and the rate card

Settings → Commercial setup (`/settings/commercial`) is where commercial and Finance record
what Pricing, Margin and the report packs compute from. ADR 0033 covers the rate card and
ADR 0035 the SOW register.

> **The repository is public.** The figures below are invented examples, chosen to be
> obviously round. Never commit a customer's real commercial terms: not here, not in a
> seed, not in a test. Real terms are entered through the page in each environment and
> live only in its database.

## Who can do what

| Action | Permission | Default roles |
| --- | --- | --- |
| See the page, the SOW register and the rate card | `commercial.read` | CEO, CFO, FinOps, Delivery lead, Commercial, Auditor, Platform administrator |
| Record a SOW or an amendment; request a margin recompute | `commercial.write` | CFO, Commercial, Platform administrator |
| Publish a rate card version | `ratecard.publish` | CFO, Commercial, Platform administrator |

Customer roles never see any of it. The database refuses them even if the matrix were
changed (ADR 0002, D10).

## Example: the dev test customer, Azure Sponsorship (`AZSPONSOR`)

These are made-up values to try the flow end to end in dev. They are not the customer's
terms.

### 1. The first SOW

1. Open **Settings → Commercial setup**.
2. Choose **Azure Sponsorship**, either in the top bar's customer selector or in the page's
   **Customer** list.
3. Select **Record the first SOW** and enter:

| Field | Example value | Why |
| --- | --- | --- |
| Reference | `SOW-EXAMPLE-001` | Any reference; it is shown with every figure that uses the SOW |
| Effective from | the first day of the month, six months ago | Margin computes the month to date, the closing month and three months before it, so start early enough to cover them |
| Term starts | the same date | |
| Term ends | empty, or a date two years later | Empty means open-ended |
| Where the agreement is recorded | `Example only: no real agreement` | In a real setup, the contract library and the document's name |
| Service scope | `Managed Azure operations (example)` | |
| Coverage / Support tier | `24x7` / `Gold` | What the pricing engine quotes the modelled cost with |
| Cloud resold as CSP | `Yes` | Whether Getronics resells the cloud. Revenue & margin derives a hosting ACV only for a CSP customer; "Not recorded" leaves it not captured |
| Contract currency | `EUR` | Margin is computed in this currency first, then converted |
| Recurring fee | `30000` | |
| Billed | `Quarterly` | Margin uses 30000 / 3 = **EUR 10,000.00 a month** |
| Target margin (%) | `25` | Gives the gap to target on the Margin page |
| Consumption uplift (%) | `5` | Prices the customer's cloud consumption in the portal. Leave it empty and the portal shows "Not yet priced". Never 0 |
| Contract values (optional) | Hosting `0`, Manage `120000`, Professional services `0` | The report pack's revenue mix |

4. Select **Record SOW**. The register shows version 1, in force today.

### 2. An amendment (optional)

1. Select **Record an amendment**. The form starts from the version in force.
2. Change only what changes. For example, effective from the first of last month,
   recurring fee `11000`, billed `Monthly`.
3. Say why, for example `Example amendment: scope extended`.
4. Select **Record amendment**. The register shows version 2. Version 1 now ends the day
   before version 2 starts, and nothing else about it changed.

### 3. Margin now

- Select **Recompute margin now**. In dev, the FinOps worker answers within a minute or two.
  The panel shows **Recomputed**, and **Open this customer's margin** shows the figures.
- Without a recompute, the daily run picks the SOW up the next day.
- Margin also needs billing data for the month. Azure Sponsorship's comes from the Azure
  cost connector (ADR 0027). Until a month has consumption, it shows "Not captured: no
  billing data", which is a gap, never a zero.

### 4. Revenue & margin

**Commercial → Revenue & margin** (`/revenue`, ADR 0038) shows the monthly report's sheet from
what was entered above. With these example values, for a month the worker has computed:

- monthly manage revenue **EUR 10,000.00**, its manage cost and gross margin from the margin
  engine;
- professional services **EUR 0.00** and manage ACV **EUR 120,000.00**, as entered;
- hosting ACV: `0` as entered. Leave it empty instead to see it derived, marked
  **Modelled**: 12 × the average monthly consumption × 1.05.

Choose a range of months, a reporting currency and **Compare with another range** to see a
month against the one before. **Export CSV** downloads the sheet with its totals.

### 5. The rate card

Version `2026-v2.14`, the pricing workbook's, is in force from 1 January 2026. Pricing quotes
on it, and margin's modelled cost uses it. To try publishing:

1. Select **Publish a new version**. The form starts from the latest version.
2. Enter a version name such as `example-2027`, an effective date in the future (after the
   latest version), changed figures (for example a blended day rate of `280`), and a reason.
   The role shares must add up to exactly 1.
3. Select **Publish version**.

A published version never changes. Don't publish an example version in an environment that
real quotes are made in: it cannot be withdrawn, only followed by another version. In dev
that is harmless. Pricing keeps quoting on the version in force until the new one's date.

## Locally

Start the stack, the API and the console, each in its own terminal. `make e2e` then runs the
browser journeys; `commercial-setup.spec.ts` onboards a customer, records a SOW and an
amendment through the page, and sees Margin fill.

```bash
make up && make seed
```

```bash
make api
```

```bash
make web
```

```bash
CLOUDOPS_ENV=local uv run python -m cloudops_connectors.finops recompute-local
```

The last command answers waiting recompute requests without Temporal; it is refused
anywhere but the local stack. A newly onboarded local customer has no billing until you give
it some:

```bash
CLOUDOPS_ENV=local uv run python -m cloudops_devdata.billing --tenant <tenant id> --daily 150 --currency EUR
```

## Deploying the FinOps worker in dev (user steps)

Merging adds the worker to the dev workers Application. Before it can start:

1. Plan and apply stack `infra/terraform/stacks/05-aks` for dev. It adds one resource,
   `azurerm_federated_identity_credential.additional["ingest-worker-finops"]`, on
   `id-cloudops-ingest-dev`, with subject
   `system:serviceaccount:platform-ingest:worker-finops`.

   Without it the pod waits on its Key Vault mount. Nothing else is needed: the ingest
   identity already reads `postgres-ingest-dsn`.
2. Merge. Argo CD syncs the Deployment, and its Sync hook creates the `finops-daily` and
   `finops-recompute` schedules.
