"""CoreStack: cost and governance findings, read-only (HLD, Integration).

Findings are kept *beside* the raw billing exports, never instead of them:
CoreStack reports cost but does not hold the SOW, so the platform still ingests
the billing exports itself (HLD). This connector writes ``cost_finding`` only;
it never writes ``cost_fact``.
"""
