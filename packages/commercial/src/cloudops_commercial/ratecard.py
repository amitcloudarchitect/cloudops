"""The rate card: versioned, effective-dated, and held in the database.

HLD M11: "versioned rate card". Finance and commercial publish a version from
Settings (ADR 0033, amending ADR 0019 decision 7); it is stored in
``cloudops.rate_card`` and its child tables, and read back whole through
``cloudops.rate_card_documents()``. A version is immutable once published: a
change of price is a new version with its own ``effective_from``, and the one
before ends where it begins (``chain``). Nothing stores an end date, so publishing
never touches a previous row. A quote or a margin figure records the version it
used, so it can be reproduced after the card has moved on.

This module holds no price. It defines the card's shape, the document the
database and the console exchange (``from_document`` / ``to_document``, every
amount a decimal string, never a float), and how to choose the version in force.
Version ``2026-v2.14``, the workbook's constants digit for digit, is seeded by
migration 0031 from ``db/migrations/data/rate_card_2026-v2.14.json``; the
``pricing-regression`` job proves that file reproduces ``reference/pricing_engine.py``
and the isolation suite proves the database returns it unchanged. Open
commercial items carried with the values, not resolved here (HLD, Decisions):

- D6: the 24x7 rows of ``service_multiplier`` carry an extra 1.1 uplift the
  16x5 and 8x5 rows do not. The workbook's values are authoritative until
  commercial confirms the published table or the computed values.
- D7: the Budapest people cost and its FX basis are awaiting Finance.
- The workbook has people cost for India and Budapest only. A ``Mixed``
  delivery location has none, so its people cost is *not captured*, never an
  invented blend.

Amounts are in the card's ``currency`` (EUR for 2026-v2.14). Conversion to a
contract currency happens where the figure is used, at the rate in force.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, cast

Coverage = Literal["8x5", "16x5", "24x7"]
SupportModel = Literal["Bronze", "Silver", "Gold"]
Location = Literal["India", "Budapest"]
Complexity = int | Literal["Case"]

# The database enums (0001): the engine prices exactly these.
COVERAGES: Final[tuple[Coverage, ...]] = ("8x5", "16x5", "24x7")
SUPPORT_MODELS: Final[tuple[SupportModel, ...]] = ("Bronze", "Silver", "Gold")
LOCATIONS: Final[tuple[Location, ...]] = ("India", "Budapest")


class RateCardError(LookupError):
    """No rate card version is in force on the date asked about."""


@dataclass(frozen=True, slots=True)
class Band:
    """A marginal consumption band: ``rate`` applies to spend inside it only."""

    lower: Decimal
    upper: Decimal | None  # None: unbounded
    rate: Decimal


@dataclass(frozen=True, slots=True)
class Slab:
    label: str
    lower: int
    upper: int
    transition_weeks: int


@dataclass(frozen=True, slots=True)
class HealthBand:
    """A margin below ``below`` (a fraction) falls in ``label``. Checked in order."""

    below: Decimal
    label: str


@dataclass(frozen=True, slots=True)
class RateCard:
    version: str
    effective_from: date
    effective_to: date | None  # exclusive; None: still in force
    source: str
    currency: str
    blended_day_rate: Decimal  # per 8-hour day
    minutes_per_day: Decimal
    complexity_minutes: Mapping[Complexity, Decimal]
    service_multiplier: Mapping[tuple[Coverage, SupportModel], Decimal]
    resource_complexity: Mapping[str, Complexity]
    # The platform's type_canonical -> the catalogue row that prices it. A type
    # with no row is an unpriced CI: counted and shown, never priced by guess.
    catalog_type_of: Mapping[str, str]
    base_pack_bands: tuple[Band, ...]
    base_pack_floor: Decimal
    base_pack_coverage_uplift: Mapping[Coverage, Decimal]
    ci_slabs: tuple[Slab, ...]
    tool_cost_per_ci: Mapping[str, Decimal]
    corestack_consumption_rate: Decimal
    people_cost: Mapping[Location, Decimal]  # per person per month, fully loaded
    working_days_per_month: Decimal
    hours_per_day: Decimal
    role_mix: Mapping[str, Decimal]
    default_tools: tuple[str, ...]
    # slab_headroom: repricing is due within max(minimum, share x slab width) CIs.
    repricing_headroom_minimum: int
    repricing_headroom_share: Decimal
    health_bands: tuple[HealthBand, ...]
    healthy_label: str

    def in_force_on(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None
                                               or day < self.effective_to)

    def health(self, margin: Decimal) -> str:
        """The band a gross margin fraction falls in (reference engine, HLD M12)."""
        for band in self.health_bands:
            if margin < band.below:
                return band.label
        return self.healthy_label


class InvalidRateCardError(ValueError):
    """A rate card document that cannot price: a missing row, a key the engine
    does not know, a share that does not add up. The message names the field."""


def _dec(value: Any, field: str, *, positive: bool = False) -> Decimal:
    # Strings only: a JSON number would arrive as a float (CLAUDE.md rule 5).
    if not isinstance(value, str):
        raise InvalidRateCardError(f"{field}: a decimal string")
    try:
        out = Decimal(value)
    except InvalidOperation:
        raise InvalidRateCardError(f"{field}: not a decimal") from None
    if not out.is_finite() or out < 0 or (positive and out == 0):
        raise InvalidRateCardError(f"{field}: must be {'above' if positive else 'at least'} 0")
    return out


def _int(value: Any, field: str) -> int:
    # A string too, like every number in the document: the API serves no bare number.
    if not isinstance(value, str) or not value.isdigit() or len(value) > 10:
        raise InvalidRateCardError(f"{field}: a whole number, at least 0, as a string")
    return int(value)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise InvalidRateCardError(f"{field}: text, 1 to 200 characters")
    return value


def _rows(doc: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    rows = doc.get(field)
    if not isinstance(rows, list) or not rows or not all(isinstance(r, Mapping) for r in rows):
        raise InvalidRateCardError(f"{field}: at least one row")
    return rows


def _unique(keys: list[Any], field: str) -> None:
    if len(set(keys)) != len(keys):
        raise InvalidRateCardError(f"{field}: a key appears twice")


def _complexity(value: Any, field: str) -> Complexity:
    if value == "Case":
        return "Case"
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise InvalidRateCardError(f"{field}: a whole number or 'Case'")


def from_document(doc: Mapping[str, Any], effective_to: date | None = None) -> RateCard:
    """A rate card from its document: the shape the database stores and returns
    (``cloudops.rate_card_documents()``) and the console publishes. Every amount
    is a decimal string. Checks that the card can price every combination the
    engine is asked for; the database checks the same again on publish."""
    try:
        start = date.fromisoformat(str(doc["effective_from"]))
    except (KeyError, ValueError):
        raise InvalidRateCardError("effective_from: an ISO date") from None
    currency = str(doc.get("currency", ""))
    if len(currency) != 3 or not currency.isupper():
        raise InvalidRateCardError("currency: three capital letters")

    complexity: dict[Complexity, Decimal] = {}
    for i, r in enumerate(_rows(doc, "complexity_minutes")):
        complexity[_complexity(r.get("complexity"), f"complexity_minutes[{i}]")] = _dec(
            r.get("minutes"), f"complexity_minutes[{i}].minutes", positive=True)
    _unique([r.get("complexity") for r in _rows(doc, "complexity_minutes")], "complexity_minutes")

    multipliers: dict[tuple[Coverage, SupportModel], Decimal] = {}
    for i, r in enumerate(_rows(doc, "service_multipliers")):
        cov, sup = r.get("coverage"), r.get("support")
        if cov not in COVERAGES or sup not in SUPPORT_MODELS:
            raise InvalidRateCardError(f"service_multipliers[{i}]: coverage and support model")
        multipliers[(cast(Coverage, cov), cast(SupportModel, sup))] = _dec(
            r.get("multiplier"), f"service_multipliers[{i}].multiplier", positive=True)
    if set(multipliers) != {(c, s) for c in COVERAGES for s in SUPPORT_MODELS}:
        raise InvalidRateCardError("service_multipliers: one per coverage and support model")

    catalogue: dict[str, Complexity] = {}
    for i, r in enumerate(_rows(doc, "catalogue")):
        c = _complexity(r.get("complexity"), f"catalogue[{i}].complexity")
        if c not in complexity:
            raise InvalidRateCardError(f"catalogue[{i}].complexity: no minutes for it")
        catalogue[_text(r.get("resource_type"), f"catalogue[{i}].resource_type")] = c
    _unique([r.get("resource_type") for r in _rows(doc, "catalogue")], "catalogue")

    type_map: dict[str, str] = {}
    for i, r in enumerate(doc.get("type_map") or []):
        row = _text(r.get("resource_type"), f"type_map[{i}].resource_type")
        if row not in catalogue:
            raise InvalidRateCardError(f"type_map[{i}].resource_type: not in the catalogue")
        type_map[_text(r.get("type_canonical"), f"type_map[{i}].type_canonical")] = row
    _unique([r.get("type_canonical") for r in doc.get("type_map") or []], "type_map")

    bands: list[Band] = []
    for i, r in enumerate(_rows(doc, "base_pack_bands")):
        lower = _dec(r.get("lower"), f"base_pack_bands[{i}].lower")
        upper = None if r.get("upper") is None else _dec(r.get("upper"),
                                                         f"base_pack_bands[{i}].upper")
        expected = bands[-1].upper if bands else Decimal(0)
        if lower != expected or (upper is not None and upper <= lower):
            raise InvalidRateCardError(f"base_pack_bands[{i}]: bands run on from 0 without gaps")
        bands.append(Band(lower, upper, _dec(r.get("rate"), f"base_pack_bands[{i}].rate")))
    if bands[-1].upper is not None:
        raise InvalidRateCardError("base_pack_bands: the last band has no upper bound")

    uplift: dict[Coverage, Decimal] = {}
    for i, r in enumerate(_rows(doc, "base_pack_coverage_uplift")):
        if r.get("coverage") not in COVERAGES:
            raise InvalidRateCardError(f"base_pack_coverage_uplift[{i}].coverage")
        uplift[cast(Coverage, r["coverage"])] = _dec(
            r.get("uplift"), f"base_pack_coverage_uplift[{i}].uplift", positive=True)
    if set(uplift) != set(COVERAGES):
        raise InvalidRateCardError("base_pack_coverage_uplift: one per coverage")

    slabs: list[Slab] = []
    for i, r in enumerate(_rows(doc, "ci_slabs")):
        s = Slab(_text(r.get("label"), f"ci_slabs[{i}].label"),
                 _int(r.get("lower"), f"ci_slabs[{i}].lower"),
                 _int(r.get("upper"), f"ci_slabs[{i}].upper"),
                 _int(r.get("transition_weeks"), f"ci_slabs[{i}].transition_weeks"))
        if s.upper < s.lower or (slabs and s.lower <= slabs[-1].upper):
            raise InvalidRateCardError(f"ci_slabs[{i}]: slabs ascend without overlapping")
        slabs.append(s)

    tools: dict[str, Decimal] = {}
    defaults: list[str] = []
    for i, r in enumerate(_rows(doc, "tools")):
        name = _text(r.get("tool"), f"tools[{i}].tool")
        tools[name] = _dec(r.get("cost_per_ci"), f"tools[{i}].cost_per_ci")
        if r.get("default") is True:
            defaults.append(name)
    _unique([r.get("tool") for r in _rows(doc, "tools")], "tools")

    people: dict[Location, Decimal] = {}
    for i, r in enumerate(_rows(doc, "people_cost")):
        if r.get("location") not in LOCATIONS:
            raise InvalidRateCardError(f"people_cost[{i}].location")
        people[cast(Location, r["location"])] = _dec(
            r.get("monthly_cost"), f"people_cost[{i}].monthly_cost", positive=True)
    _unique([r.get("location") for r in _rows(doc, "people_cost")], "people_cost")

    roles: dict[str, Decimal] = {}
    for i, r in enumerate(_rows(doc, "roles")):
        roles[_text(r.get("role"), f"roles[{i}].role")] = _dec(r.get("share"),
                                                               f"roles[{i}].share")
    _unique([r.get("role") for r in _rows(doc, "roles")], "roles")
    if sum(roles.values(), Decimal(0)) != 1:
        raise InvalidRateCardError("roles: the shares add up to 1")

    health: list[HealthBand] = []
    for i, r in enumerate(_rows(doc, "health_bands")):
        below = _dec(r.get("below"), f"health_bands[{i}].below")
        if health and below <= health[-1].below:
            raise InvalidRateCardError(f"health_bands[{i}]: thresholds ascend")
        health.append(HealthBand(below, _text(r.get("label"), f"health_bands[{i}].label")))

    return RateCard(
        version=_text(doc.get("version"), "version"),
        effective_from=start,
        effective_to=effective_to,
        source=_text(doc.get("source"), "source"),
        currency=currency,
        blended_day_rate=_dec(doc.get("blended_day_rate"), "blended_day_rate", positive=True),
        minutes_per_day=_dec(doc.get("minutes_per_day"), "minutes_per_day", positive=True),
        complexity_minutes=complexity,
        service_multiplier=multipliers,
        resource_complexity=catalogue,
        catalog_type_of=type_map,
        base_pack_bands=tuple(bands),
        base_pack_floor=_dec(doc.get("base_pack_floor"), "base_pack_floor"),
        base_pack_coverage_uplift=uplift,
        ci_slabs=tuple(slabs),
        tool_cost_per_ci=tools,
        corestack_consumption_rate=_dec(doc.get("corestack_consumption_rate"),
                                        "corestack_consumption_rate"),
        people_cost=people,
        working_days_per_month=_dec(doc.get("working_days_per_month"),
                                    "working_days_per_month", positive=True),
        hours_per_day=_dec(doc.get("hours_per_day"), "hours_per_day", positive=True),
        role_mix=roles,
        default_tools=tuple(defaults),
        repricing_headroom_minimum=_int(doc.get("repricing_headroom_minimum"),
                                        "repricing_headroom_minimum"),
        repricing_headroom_share=_dec(doc.get("repricing_headroom_share"),
                                      "repricing_headroom_share"),
        health_bands=tuple(health),
        healthy_label=_text(doc.get("healthy_label"), "healthy_label"),
    )


def to_document(card: RateCard) -> dict[str, Any]:
    """The inverse of ``from_document``: what a new version is published as."""
    s = str
    return {
        "version": card.version, "effective_from": card.effective_from.isoformat(),
        "source": card.source, "currency": card.currency,
        "blended_day_rate": s(card.blended_day_rate), "minutes_per_day": s(card.minutes_per_day),
        "working_days_per_month": s(card.working_days_per_month),
        "hours_per_day": s(card.hours_per_day), "base_pack_floor": s(card.base_pack_floor),
        "corestack_consumption_rate": s(card.corestack_consumption_rate),
        "repricing_headroom_minimum": s(card.repricing_headroom_minimum),
        "repricing_headroom_share": s(card.repricing_headroom_share),
        "healthy_label": card.healthy_label,
        "complexity_minutes": [{"complexity": s(k), "minutes": s(v)}
                               for k, v in card.complexity_minutes.items()],
        "service_multipliers": [{"coverage": c, "support": m, "multiplier": s(v)}
                                for (c, m), v in card.service_multiplier.items()],
        "catalogue": [{"resource_type": k, "complexity": s(v)}
                      for k, v in card.resource_complexity.items()],
        "type_map": [{"type_canonical": k, "resource_type": v}
                     for k, v in card.catalog_type_of.items()],
        "base_pack_bands": [{"lower": s(b.lower), "upper": None if b.upper is None
                             else s(b.upper), "rate": s(b.rate)} for b in card.base_pack_bands],
        "base_pack_coverage_uplift": [{"coverage": k, "uplift": s(v)}
                                      for k, v in card.base_pack_coverage_uplift.items()],
        "ci_slabs": [{"label": x.label, "lower": s(x.lower), "upper": s(x.upper),
                      "transition_weeks": s(x.transition_weeks)} for x in card.ci_slabs],
        "tools": [{"tool": k, "cost_per_ci": s(v), "default": k in card.default_tools}
                  for k, v in card.tool_cost_per_ci.items()],
        "people_cost": [{"location": k, "monthly_cost": s(v)}
                        for k, v in card.people_cost.items()],
        "roles": [{"role": k, "share": s(v)} for k, v in card.role_mix.items()],
        "health_bands": [{"below": s(b.below), "label": b.label} for b in card.health_bands],
    }


def chain(documents: Iterable[Mapping[str, Any]]) -> tuple[RateCard, ...]:
    """Every published version, oldest first, each ending where the next begins.
    Nothing stores an end date: publishing a version never touches the one before."""
    docs = sorted(documents, key=lambda d: str(d.get("effective_from")))
    starts = [date.fromisoformat(str(d["effective_from"])) for d in docs]
    return tuple(from_document(d, starts[i + 1] if i + 1 < len(docs) else None)
                 for i, d in enumerate(docs))


def rate_card_on(day: date, cards: tuple[RateCard, ...]) -> RateCard:
    """The version in force on ``day``. Raises rather than guessing."""
    found = [c for c in cards if c.in_force_on(day)]
    if len(found) != 1:
        raise RateCardError(f"{len(found)} rate card versions in force on {day.isoformat()}")
    return found[0]


def rate_card(version: str, cards: tuple[RateCard, ...]) -> RateCard:
    for c in cards:
        if c.version == version:
            return c
    raise RateCardError(f"no rate card version {version!r}")
