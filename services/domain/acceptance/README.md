# Report pack acceptance (step 17)

The acceptance test for the reporting pack (`/step17`): regenerate a recent month and
diff it against the manually assembled version, line by line, including currency
presentation.

**Status: pending.** No manually assembled pack is in the repository (ADR 0019). The
harness is built and tested (`cloudops_domain.reporting.acceptance`); the comparison
itself waits for a real pack.

## Procedure

1. Pick a recent month and a customer whose pack was assembled by hand.
2. Extract the manual pack's text, one printed line per line, into `manual.txt`. Keep
   amounts exactly as printed (`EUR 1,234.56`, `€1,234.56` and `1.234,56 €` are
   different lines on purpose).
3. Generate the same month in the console (Reports, Generate pack), have a person write
   each narrative, and download its text:
   `GET /v1/reports/packs/{id}/text` (the PDF's own lines).
4. Compare:

   ```bash
   uv run python -m cloudops_domain.reporting.acceptance generated.txt manual.txt
   ```

   It exits 0 only on a line-by-line match, and lists every line whose amount is
   presented differently.

## Known differences to expect

- The financial dashboard and revenue mix print "Not captured" until step 16 (FinOps,
  pricing and margin) is wired in through `FinancialSections`.
- CSAT prints "Not captured" until a survey source is connected.
- The generated pack prints its data-as-of time and version in the subtitle.
- Currency is presented as `EUR 1,234.56` (`cloudops_domain.reporting.money.present`):
  if the manual pack differs, that one function changes.

Customer data must not be committed here: a real pack stays outside the repository,
or is pseudonymised first, as golden-set cases are (`evals/README.md`).
