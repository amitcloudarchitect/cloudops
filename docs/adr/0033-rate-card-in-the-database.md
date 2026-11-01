# ADR 0033: The rate card is published from Settings, not released in code

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar ("Editable in Settings")
- **Amends:** ADR 0019, decision 7 (the rate card in `ratecard.py`)
- **Implements:** HLD M11 (versioned rate card); migration `0031_rate_card` (expand),
  `cloudops_commercial.ratecard` (the document codec), `cloudops_api.commercial`
  (`/v1/commercial/rate-cards`, the quote and the rate card view),
  `cloudops_connectors.finops.jobs` (margin), the `pricing-regression` CI job, the
  money-literal gate's allowlist.

## Context

ADR 0019 ported the workbook's pricing engine into `packages/commercial` and put its
constants in `ratecard.py`: one immutable, effective-dated `RateCard` per version, the
only file the money-literal gate allowed a price in, with the `pricing-regression` job
proving the port equals the reference engine. A price change was therefore a pull
request and a release.

The user wants pricing set up through the platform. Finance and commercial publish a
new rate card version from Settings, beside the SOW register (Settings → Commercial
setup), in the same way they correct an FX rate. Two things from ADR 0019 must still
hold:

- a version never changes once published, and a figure names the version it used;
- the workbook's version still reproduces the reference engine.

## Decisions

1. **The rate card is data in the database.** `cloudops.rate_card` holds one row per
   version with the card's single values. Eleven child tables hold its parts: complexity
   minutes, service multipliers, the catalogue, the platform-type map, base pack bands,
   coverage uplift, CI slabs, tools, people cost by location, the role mix, and margin
   health bands. Each part keeps its position, because the engine reads bands, slabs,
   health bands and the team in order. `cloudops.rate_card_documents()` returns each
   version whole, as the JSON document that `ratecard.from_document` reads. The engine
   (`pricing.py`, `margin.py`) is unchanged: it still takes a `RateCard`.

2. **Immutable, effective-dated, never edited.**
   - `effective_from` is unique, and no end date is stored. A version ends where the next
     one begins, worked out when the cards are read (`ratecard.chain`), so publishing
     never touches an earlier row.
   - A trigger refuses UPDATE, DELETE and TRUNCATE on every rate card table, for the
     owner too.
   - Row-level security already leaves the owner nothing to update, because there is no
     UPDATE or DELETE policy. The trigger is the guard where RLS does not apply.

3. **A new version takes effect after today, and after the latest version.** Margin
   prices a month on the version in force on the month's first day, and quotes take an
   `on` date. A future-only rule therefore means publishing never reprices a closed month
   or the current one, so no margin is restated by a price change. To correct a mistake,
   publish the next version. History stays as it was.

4. **Who publishes: `ratecard.publish`, a commercial matrix permission.**
   - By default CFO, Commercial and Platform administrator hold it. FinOps reads the card
     (`commercial.read`) but does not publish it.
   - Because the permission is `is_commercial`, 0004's trigger refuses it to any role
     class outside the `commercial_visibility` list, so never to a customer role
     (ADR 0002, D10).
   - The only write path is `access.publish_rate_card(actor, document, reason)`. It is
     SECURITY DEFINER, owned by `cloudops_access_admin` and executable by `cloudops_app`.
     It checks the matrix itself, whatever the API did, and records who published, when
     and why on the row.
   - The request is also in `audit.action_log`, through the API's audit stage.

5. **Two locks on reading.** Every table is FORCE RLS with the restrictive
   `commercial_visibility` policy, so an engineer scope or any role string outside the
   list reads no row. `cloudops_customer` holds no grant on any table or on
   `rate_card_documents()`, so a customer scope gets `permission denied`. The rate card is
   platform-wide reference data with no `tenant_id`, like `fx_rate`. That is not an
   exception to "every table carrying customer data has a tenant_id": it carries none.

6. **Rates keep the workbook's precision, not `numeric(18,6)`.** The inputs are
   `cloudops.rate_input`, an unconstrained `numeric` of at most 16 decimal places. The
   workbook's day rate is 266.9932626632895 and India's people cost is
   1786.9079747305266; truncating them to six places changes the quote, and the
   regression gate would fail. They are factors the engine multiplies, not stored amounts
   of money. Every figure computed from them is stored at `numeric(18,6)` with its
   currency, as before (CLAUDE.md rule 5). The amounts carry the version's `currency`.
   Every amount is a decimal string in the document, and the API accepts no JSON number
   for one, so no float is ever involved.

7. **Version 1 is the workbook, proven twice.**
   - Migration 0031 seeds `2026-v2.14` from `db/migrations/data/rate_card_2026-v2.14.json`,
     which was generated from `ratecard.py`'s constants before they were removed.
   - The `pricing-regression` job runs the reference engine, then the port on that file
     (equal at six places on the reference case and across every coverage, support model
     and location). Then it migrates a database and proves the published version 1 is
     that file digit for digit and quotes the reference case identically
     (`db/tests/test_rate_card.py`).
   - Later versions are new prices by design, so they are not compared with the workbook.
     They must pass the engine's structural checks on publish, in `from_document` and
     again in the database: every coverage × support multiplier, every coverage uplift,
     bands running from 0 without gaps to one open-ended band, ascending slabs and health
     bands, role shares adding up to exactly 1, people cost for India and Budapest only,
     and every catalogue row's complexity having minutes.

8. **The money-literal gate is narrowed, not dropped.** `ratecard.py` now holds no price,
   so it is removed from `money_literal_allowlist.txt`. `packages/commercial` is covered
   like any other product code, and a price written into the engine fails the build. The
   seed is JSON under `db/migrations/data/`, data rather than code. The gate never
   scanned migrations, and the regression job checks that file against the reference
   engine. The remaining entries are the AI rate card, the synthetic dataset and the
   evaluation's cost band.

## Consequences

- A price change is a Settings action by Finance or commercial, effective from a future
  date, and needs no release. Its review is the publisher's own: the platform records
  who, when and why, and shows every version.
- HLD D6 (the 24x7 multiplier) and D7 (Budapest people cost) are still open. They now
  close by publishing a version rather than by a code change.
- The quote endpoint and the FinOps worker read the card from the database under the
  caller's scope. A scope that cannot read it gets "no rate card in force", never a
  default card.
- Quotes stay in the rate card's currency (ADR 0019, decided for review). A version in
  another currency is possible, and margin converts from the card's currency as it
  already does.
- Not built: a per-customer charge-out card (role day rates for T&M work, in the contract
  currency). Nothing reads one today, because margin uses the SOW's recurring fee. It is
  a follow-up if T&M SOWs need it.
