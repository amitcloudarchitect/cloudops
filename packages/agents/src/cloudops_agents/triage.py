"""The deterministic half of stages 1 to 3 (HLD, Pipeline: "fails safe by").

Pure functions, used by the Temporal workflow and by the evaluation harness
alike, so what is measured is what runs. The model fills in content; these
decide what happens with it (CLAUDE.md rule 3). Every way out of them except
one leads to a human.

Stage 1, intake (``intake``): only open incidents and service requests are
recognised. Anything else goes to a human.

Stage 2, after classification (``after_classification``), to a human when:
- the classifier declined (low confidence);
- the action class is E ("never agent-initiated");
- the ticket contains instruction-like content;
- the ticket names a different customer from the one it was ingested for;
- the CI the classifier reported is not in the ticket text.

Stage 3, after enrichment (``after_enrichment``), to a human when:
- the enricher declined;
- the resource could not be found;
- the bundle cites anything that is not in the tool results it was given.

Otherwise the result is ``ready_for_planning``, which is where this step stops.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

Route = Literal["continue", "human", "ready_for_planning"]

RECOGNISED_KINDS: Final = frozenset({"incident", "service_request"})
CLOSED_STATES: Final = frozenset({"closed", "resolved", "cancelled", "canceled"})
LEGAL_SUFFIXES: Final = frozenset({"limited", "ltd", "bv", "gmbh", "sa", "plc", "inc", "group",
                                   "holdings", "services"})

# Instruction-like content in ticket fields. A heuristic, deliberately broad: a
# false positive costs an engineer a glance; a false negative is what the
# schema and the later stages exist for. It reads the input, never model output.
INJECTION_PATTERNS: Final = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instructions?|rules|prompt|guidelines)\b",
    r"\byou are (now )?(in )?(an? )?(admin|developer|system|god|unrestricted)\b",
    r"(^|[\s:;])system\s*:",
    r"\b(note|message|instructions?) (to|for) (the )?(ai|assistant|model|bot|agent|classifier)\b",
    r"</?\s*(data|request|system|instruction)s?\b",
    r"\b(classify|mark|label|set|treat|rate) (this|it|me|the ticket)( ticket)? as "
    r"(class\b|priority\b|risk\b|low\b|read[- ]?only|approved|resolved|safe|harmless)",
    r"\bpre-?approved\b",
    r"\b(new|updated) instructions\b",
))


@dataclass(frozen=True)
class Decision:
    route: Route
    reasons: tuple[str, ...] = ()


def injection_signals(fields: Mapping[str, Any]) -> list[str]:
    hits: list[str] = []
    for name, value in fields.items():
        if isinstance(value, str):
            for pattern in INJECTION_PATTERNS:
                if pattern.search(value):
                    hits.append(name)
                    break
    return hits


def _company_key(name: str) -> str:
    tokens = re.split(r"\s+", name.lower().strip())
    return "".join(t for t in (re.sub(r"[^a-z0-9]", "", t) for t in tokens)
                   if t and t not in LEGAL_SUFFIXES)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def intake(ticket: Mapping[str, Any]) -> Decision:
    kind = str(ticket.get("kind") or "")
    state = str(ticket.get("state") or "").lower()
    if kind not in RECOGNISED_KINDS:
        return Decision("human", ("unrecognised_type",))
    if state in CLOSED_STATES:
        return Decision("human", ("not_open",))
    if not str(ticket.get("short_description") or "").strip():
        return Decision("human", ("empty_ticket",))
    return Decision("continue")


def after_classification(
    ticket: Mapping[str, Any],
    *,
    status: str,
    output: Mapping[str, Any] | None,
    tenant_names: Iterable[str],
) -> Decision:
    reasons: list[str] = []
    if status != "answered" or output is None:
        return Decision("human", ("low_confidence",))
    if output.get("action_class") == "E":
        reasons.append("class_e_never_agent_initiated")
    if injection_signals({k: ticket.get(k) for k in ("short_description", "configuration_item",
                                                       "close_notes")}):
        reasons.append("instruction_like_content")
    named = output.get("customer")
    if named:
        known = {_company_key(n) for n in tenant_names if n}
        if _company_key(str(named)) not in known:
            reasons.append("customer_mismatch")
    ci = output.get("affected_ci")
    if ci:
        text = _norm(" ".join(str(ticket.get(k) or "")
                              for k in ("short_description", "configuration_item")))
        if _norm(str(ci)) not in text:
            reasons.append("ungrounded_ci")
    return Decision("human", tuple(reasons)) if reasons else Decision("continue")


@dataclass(frozen=True)
class Grounding:
    grounded: bool
    missing: tuple[str, ...] = field(default_factory=tuple)


def grounding(claims: Sequence[str], evidence: Sequence[str]) -> Grounding:
    """Every reference the bundle makes must appear in a tool result it saw."""
    corpus = "\n".join(evidence)
    missing = tuple(c for c in claims if c and c not in corpus)
    return Grounding(not missing, missing)


def bundle_claims(bundle: Mapping[str, Any]) -> list[str]:
    claims: list[str] = []
    for key in ("related_tickets", "prior_resolutions", "runbook_candidates"):
        claims.extend(str(v) for v in bundle.get(key) or [])
    return claims


def after_enrichment(
    *, status: str, bundle: Mapping[str, Any] | None, evidence: Sequence[str]
) -> Decision:
    if status != "answered" or bundle is None:
        return Decision("human", ("low_enrichment_confidence",))
    reasons: list[str] = []
    if not bundle.get("resource_found"):
        reasons.append("resource_not_found")
    g = grounding(bundle_claims(bundle), evidence)
    if not g.grounded:
        reasons.append("ungrounded_context")
    return Decision("human", tuple(reasons)) if reasons else Decision("ready_for_planning")


def evidence_ids(evidence: Sequence[str]) -> set[str]:
    """Every string value in the tool results, for leak checks."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str):
            found.add(value)

    for item in evidence:
        try:
            walk(json.loads(item))
        except ValueError:
            continue
    return found
