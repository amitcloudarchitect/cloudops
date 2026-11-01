"""Suggested questions follow the screen and the caller's permissions (ADR 0029)."""

from __future__ import annotations

from cloudops_api.assistant import suggestions


def test_suggestions_never_offer_what_the_caller_cannot_see() -> None:
    customer = frozenset({"tickets.read", "inventory.read"})
    finance = frozenset({"commercial.read", "cost.read"})
    assert not any("margin" in q.lower() and "band" not in q.lower()
                   for q in suggestions("margin_customer", customer, "account"))
    assert any("modelled" in q for q in suggestions("margin_customer", finance, "account"))
    assert suggestions("ticket", customer, "account")[0].startswith("Summarise this ticket")
    assert len(suggestions("customer", finance | customer, "account")) <= 4


def test_help_mode_offers_help_only() -> None:
    staff = frozenset({"tickets.read", "commercial.read"})
    assert suggestions("ticket", staff, "help") == suggestions("other", staff, "help")
    assert all("ticket" not in q.lower() for q in suggestions("ticket", staff, "help"))
