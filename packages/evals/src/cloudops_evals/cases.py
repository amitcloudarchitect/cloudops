"""The golden set: tickets with known-correct outcomes (LLD, Evaluation).

Cases live in ``evals/golden/*.jsonl``, one JSON object per line, versioned
with the code. Each says which synthetic tenant it belongs to, the ticket as
ServiceNow would deliver it, and what a correct pipeline does with it.

Kinds:

- **classification:** a well-formed ticket. Scored on intent, affected CI and
  action class, and on reaching ``ready_for_planning``.
- **refusal:** a ticket that must go to a human (vague, class E, unrecognised,
  the wrong customer). Scored on routing.
- **injection:** instructions planted in ticket fields. Zero tolerance:
  the pipeline must not do what they say.
- **cross_tenant:** a ticket that baits retrieval or tools towards another
  customer's data. Zero tolerance for any foreign item reaching the evidence.

**New cases from production corrections.** When an engineer overrides an
agent, ``candidate_from_correction`` turns the ticket and the corrected labels
into a case with status ``candidate``. It is redacted by the gateway's rules,
attributed and dated, and ignored by CI until a person has checked the text
is safe to commit, pseudonymised it, and set ``status`` to ``active``. Real
ticket text never reaches the repository unreviewed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cloudops_gateway.redaction import redact

Kind = Literal["classification", "refusal", "injection", "cross_tenant"]
Route = Literal["ready_for_planning", "human"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Ticket(_Closed):
    number: str = Field(pattern=r"^[A-Z]+-?\d+$")
    kind: str = "incident"
    state: str = "New"
    company: str
    short_description: str
    configuration_item: str | None = None
    priority: str | None = None
    close_notes: str | None = None


class Expected(_Closed):
    route: Route
    intent: str | None = None
    action_class: Literal["A", "B", "C", "D", "E"] | None = None
    ci: str | None = None
    reason: str | None = None  # the guardrail expected to route it, if any


class Plant(_Closed):
    """Another tenant's content, put in place before the case runs, that the
    case tries to reach."""

    tenant: str
    ref: str
    text: str


class GoldenCase(_Closed):
    id: str = Field(pattern=r"^[a-z]+-\d{3}$")
    kind: Kind
    tenant: str
    ticket: Ticket
    expected: Expected
    plant: Plant | None = None
    # Strings from another tenant that must never appear in what tools returned.
    leak_markers: list[str] = Field(default_factory=list)
    status: Literal["active", "candidate"] = "active"
    source: Literal["seed", "production_correction"] = "seed"
    added: date
    added_by: str = "step08"
    notes: str = ""


def load_cases(directory: Path, *, include_candidates: bool = False) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                case = GoldenCase.model_validate_json(line)
            except ValueError as exc:
                raise ValueError(f"{path.name}:{lineno}: {exc}") from None
            if case.id in seen:
                raise ValueError(f"{path.name}:{lineno}: duplicate case id {case.id}")
            seen.add(case.id)
            if case.status == "active" or include_candidates:
                cases.append(case)
    return cases


def candidate_from_correction(
    *,
    case_id: str,
    tenant_key: str,
    ticket: Mapping[str, Any],
    corrected: Mapping[str, Any],
    corrected_by: str,
    on: date,
    personal_terms: Iterable[str] = (),
    kind: Kind = "classification",
) -> GoldenCase:
    """A candidate case from an engineer's override of an agent's output.
    Every text field is redacted; the case stays a candidate until reviewed."""
    clean = {k: (redact(v, personal_terms).text if isinstance(v, str) else v)
             for k, v in ticket.items()}
    return GoldenCase(
        id=case_id, kind=kind, tenant=tenant_key, ticket=Ticket.model_validate(clean),
        expected=Expected.model_validate(dict(corrected)), status="candidate",
        source="production_correction", added=on, added_by=corrected_by,
        notes="From a production correction. Review, pseudonymise, then set status to active.",
    )


def dump_case(case: GoldenCase) -> str:
    return json.dumps(case.model_dump(mode="json", exclude_defaults=False), sort_keys=True)
