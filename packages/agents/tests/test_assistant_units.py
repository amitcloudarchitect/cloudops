"""The assistant's deterministic parts (ADR 0029): guards, the number check, the
help pages' format, and which tools a caller is offered."""

from __future__ import annotations

import pytest

from cloudops_agents.assistant import (
    ANSWER_TOOLS,
    asks_to_compare,
    names_other_customer,
    ungrounded_numbers,
)
from cloudops_agents.help_corpus import HelpFormatError, load_pages, parse_page


@pytest.mark.parametrize("question", [
    "Compare our SLA with Bravo", "How do we rank against other customers?",
    "Show all customers' incidents", "Benchmark us versus the market",
    "which customer has the most tickets?", "us vs. them",
])
def test_comparisons_are_caught(question: str) -> None:
    assert asks_to_compare(question)


@pytest.mark.parametrize("question", [
    "How is SLA attainment this month?", "Summarise this ticket.",
    "What does the customer overview show?", "Is the margin figure modelled?",
])
def test_ordinary_questions_are_not(question: str) -> None:
    assert not asks_to_compare(question)


def test_naming_another_customer_is_a_whole_word_match() -> None:
    others = ["Bravo Holdings", "Charlie", "AB"]
    assert names_other_customer("What about bravo holdings?", others)
    assert names_other_customer("charlie's tickets", others)
    assert not names_other_customer("Charliestown office", others)
    assert not names_other_customer("ab testing", others)  # too short to match


def test_numbers_must_come_from_the_evidence() -> None:
    evidence = ['{"breached": 3, "price": "1250.500000", "margin_fraction": "0.2153"}']
    assert ungrounded_numbers("3 tickets breached; price 1250.5 EUR", evidence) == []
    assert ungrounded_numbers("Margin was 21.53%, about 21.5%", evidence) == []
    assert ungrounded_numbers("That is 1253.5 in total", evidence) == ["1253.5"]
    assert ungrounded_numbers("INC0012345 is open", evidence) == []  # an id, not a number


def test_the_help_corpus_loads_and_splits_by_audience() -> None:
    pages = load_pages()
    kinds = {p.kind for p in pages}
    assert kinds == {"platform_help", "platform_help_staff"}
    assert len({p.ref for p in pages}) == len(pages)
    staff = {p.page for p in pages if p.kind == "platform_help_staff"}
    assert "margin-and-pricing" in staff


def test_the_help_corpus_holds_nothing_internal() -> None:
    """The repository is public and the pages reach customers (ADR 0029)."""
    banned = ("row-level", "terraform", "key vault", "kubernetes", "adr ", "lld", "hld",
              "connection string", "cloudops_", "private endpoint", "set local")
    text = "\n".join(p.body.lower() for p in load_pages())
    assert [b for b in banned if b in text] == []


@pytest.mark.parametrize(("source", "problem"), [
    ("no title", "first line"),
    ("# T\naudience: everyone\n## S\nx", "second line"),
    ("# T\naudience: all\nstray\n## S\nx", "before the first section"),
    ("# T\naudience: all\n## S\n\n## S\nx", "empty"),
])
def test_malformed_help_pages_are_refused(source: str, problem: str) -> None:
    with pytest.raises(HelpFormatError, match=problem):
        parse_page("t", source)


def test_the_answerer_has_read_tools_only() -> None:
    from cloudops_gateway.registry import ToolKind
    from cloudops_gateway.tools import TOOLS

    by_name = {t.name: t for t in TOOLS}
    assert set(by_name) >= ANSWER_TOOLS
    assert all(by_name[n].kind is ToolKind.READ for n in ANSWER_TOOLS)
    assert by_name["get_margin"].permission == "commercial.read"
