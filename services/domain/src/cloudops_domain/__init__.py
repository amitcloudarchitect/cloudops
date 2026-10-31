"""cloudops_domain — knowledge, assurance and reporting (step 17, ADR 0020).

Deterministic domain logic over the platform database. It reaches the database
only through ``cloudops_tenancy`` connections it is handed, and never calls a
model: AI drafting lives in ``cloudops_agents`` and writes only draft columns.

- ``knowledge``   document kinds, staleness, gap analysis and citations (M20)
- ``assurance``   control library, evidence routing, scoring, signed packs (M14, M15)
- ``reporting``   monthly pack sections, currency presentation, PDF, diff (M19)
"""
