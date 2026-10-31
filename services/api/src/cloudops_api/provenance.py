"""Provenance on every metric (HLD design principle 2; lifecycle stage 6).

A number in a response is a ``Metric``, never a bare int, float or Decimal.
Every field is required — there is no default to forget — and
``tests/test_api_provenance.py`` walks every response model the API serves
and fails on a numeric field outside a ``Metric``.

Money is ``Decimal`` and serialises as a string, never a float (CLAUDE.md
rule 5). Its ``unit`` is the ISO currency code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator

V = TypeVar("V", Decimal, int)


class ApiModel(BaseModel):
    """Base for every response body: closed, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Metric(ApiModel, Generic[V]):  # noqa: UP046 - pydantic generic model
    value: V
    unit: str
    source_system: str
    as_of: datetime
    query_ref: str
    is_modelled: bool

    @field_validator("as_of")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return v

    @field_validator("value", mode="before")
    @classmethod
    def _never_float(cls, v: object) -> object:
        # Pydantic would quietly turn 0.1 into Decimal('0.1000000000000000055...').
        # Money never passes through a float (CLAUDE.md rule 5), so refuse it.
        if isinstance(v, float | bool):
            raise ValueError("metric values are Decimal or int, never float or bool")
        return v

    @field_validator("value", mode="after")
    @classmethod
    def _plain_zero(cls, v: V) -> V:
        # A zero with more than six places (a numeric(14,8) zero reads back as
        # Decimal("0E-8")) serialises in exponent form; carry it at six places.
        # Every other value keeps its scale.
        if isinstance(v, Decimal) and v == 0 and "E" in str(v):
            return Decimal("0.000000")
        return v

    @field_validator("unit", "source_system", "query_ref")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def query_ref(name: str, version: int, **params: object) -> str:
    """A stable reference to the query and parameters that produced a figure.

    The same question always yields the same reference, so a number can be
    traced and re-run. Parameters are hashed rather than embedded, so the
    reference never carries a tenant id or a customer name.
    """
    canonical = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{name}.v{version}:{digest}"
