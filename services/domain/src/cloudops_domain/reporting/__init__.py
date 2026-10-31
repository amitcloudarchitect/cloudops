"""Reporting: the monthly pack per customer (HLD M19; ADR 0019).

- ``sections``   the sections, generated from live data; the step 16 seam
- ``money``      presenting money: rounded once, in its currency
- ``render``     the pack as lines, shared by the PDF and the acceptance diff
- ``pdf``        a dependency-free PDF writer
- ``portfolio``  consolidation in the reporting currency, from the same rows
- ``acceptance`` the line-by-line diff against the manually assembled pack
"""
